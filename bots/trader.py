"""BOT 2 — TRADER (esecuzione), gira 24/7 ogni pochi minuti.

Ciclo:
  1. stato del conto + circuit breaker (kill switch se drawdown eccessivo)
  2. gestione delle posizioni aperte: trailing stop (software + emergenza sul broker),
     uscite per trend/direttiva/tempo
  3. nuovi ingressi con tre strategie (trend long, short ETF, rimbalzo da capitolazione):
     crypto sempre, azioni/ETF solo a mercato aperto, mai a ridosso di eventi macro
  4. strategie ombra: varianti simulate sugli stessi dati, per confronto
"""
import traceback
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

from tradebot import db, ladder, macro, regime, risk, signals
from tradebot.broker import get_broker, is_crypto, norm
from tradebot.config import ENV, fmt_price, require_keys, deep_merge, load_settings, setup_logging

log = setup_logging("trader")
CFG = load_settings()
require_keys(log)
broker = get_broker(CFG, writer=True)
TF = CFG["timeframes"]
UNIVERSE = CFG["universe"]["crypto"] + CFG["universe"]["stocks"]


def directive_for(sym: str) -> tuple[dict | None, float]:
    d = db.latest_directive()
    if not d:
        return None, 0.5
    age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(d["_ts"])).total_seconds() / 3600
    g = d.get("global_risk_multiplier", 1.0) * (0.5 if age_h > 12 else 1.0)  # direttiva vecchia → prudenza
    return next((s for s in d.get("symbols", []) if s["symbol"] == sym), None), g


ICON = {"trend": "🔵", "short": "🟣", "capitulation": "🟡"}
LABEL = {"trend": "LONG", "short": "SHORT", "capitulation": "RIMBALZO"}


def record_close(t: dict, px: float, reason: str):
    sgn = -1 if t.get("side") == "short" else 1
    pnl = sgn * (px - t["entry_price"]) * t["qty"]
    one_r = abs(t["entry_price"] - t["initial_stop"])
    r = sgn * (px - t["entry_price"]) / one_r if one_r > 0 else 0
    db.update_trade(t["id"], exit_price=px, exit_ts=db.now_iso(), exit_reason=reason, pnl=pnl, r_multiple=r, status="closed")
    if t.get("strategy") == "capitulation":
        db.kv_set(f"cap_cooldown:{t['symbol']}", datetime.now(timezone.utc).timestamp()
                  + CFG["capitulation"].get("cooldown_hours", 48) * 3600)
    db.kv_set(f"backup_level:{t['id']}", None)
    db.event("trade", f"{'🟢' if pnl >= 0 else '🔴'} <b>CHIUSO {LABEL.get(t.get('strategy') or 'trend', '')} {t['symbol']}</b>"
                      f" @ {fmt_price(px)}\nP&L {pnl:+,.2f}$ ({r:+.2f}R) · motivo: {reason}")
    log.info("chiuso %s %s pnl=%.2f motivo=%s", t.get("side"), t["symbol"], pnl, reason)


def close_trade(t: dict, reason: str):
    record_close(t, broker.close(t["symbol"]), reason)


def manage_open(positions: dict):
    rk = CFG["risk"]
    for t in db.open_trades():
        sym = t["symbol"]
        short = t.get("side") == "short"
        if norm(sym) not in positions:   # chiusa fuori dal ciclo: stop d'emergenza, manuale o broker
            filled = broker.order_filled(t.get("backup_order_id"))
            record_close(t, filled or broker.last_price(sym),
                         "stop di emergenza sul broker" if filled else "chiusa esternamente")
            continue
        if not is_crypto(sym) and not broker.market_open():
            continue
        price = broker.last_price(sym)
        atr4 = float(regime.medium_term(broker.bars(sym, TF["medium"]))["atr"])
        lt = regime.long_term(broker.bars(sym, TF["long"]))
        bars_since = int((datetime.now(timezone.utc) - datetime.fromisoformat(t["entry_ts"])).total_seconds() // 14400)
        dsym, _ = directive_for(sym)
        reason, new_stop = signals.evaluate_exit(t, price, atr4, bars_since, lt["label"], dsym, CFG)
        upd = {"highest": max(t["highest"], price), "lowest": min(t["lowest"], price), "stop": new_stop}
        if reason:
            db.update_trade(t["id"], **upd)
            close_trade({**t, **upd}, reason)
            continue
        moved = (t["stop"] - new_stop) if short else (new_stop - t["stop"])
        if moved > abs(t["stop"]) * 0.002:
            log.info("%s stop spostato %.4g → %.4g", sym, t["stop"], new_stop)
            # lo stop di emergenza segue a distanza di sicurezza (sposta solo se guadagna ≥ 1 ATR)
            if rk.get("broker_backup_stop") and t.get("backup_order_id"):
                gap = rk["backup_stop_extra_atr"] * atr4 * (-1 if short else 1)
                bk = new_stop - gap
                cur = db.kv_get(f"backup_level:{t['id']}") or (t["initial_stop"] - gap)
                if (cur - bk if short else bk - cur) >= atr4:
                    upd["backup_order_id"] = broker.move_backup_stop(t["backup_order_id"], sym, bk)
                    db.kv_set(f"backup_level:{t['id']}", bk)
        db.update_trade(t["id"], **upd)


def size_mult(ev: dict, dsym: dict | None, gmult: float, cb: dict, vmult: float) -> float:
    base = cb["risk_mult"] * gmult * vmult
    if ev["strategy"] == "capitulation":       # rischio fisso e piccolo, indipendente dalla convinzione
        return cb["risk_mult"] * vmult * CFG["capitulation"]["risk_pct"] / CFG["risk"]["risk_per_trade_pct"]
    conv = dsym["risk_multiplier"] * (0.5 + dsym["conviction"]) if dsym else 0.5
    if ev["strategy"] == "short":
        conv *= CFG["shorts"].get("risk_mult", 0.75)
    return base * conv


def look_for_entries(acct: dict, positions: dict, cb: dict, evals: dict):
    rk = CFG["risk"]
    if len(positions) >= rk["max_open_positions"]:
        return
    blackout = macro.macro_blackout(CFG)
    if blackout:
        if db.kv_get("last_blackout") != blackout:
            db.kv_set("last_blackout", blackout)
            db.event("macro", f"📅 Pausa ingressi per evento macro: {blackout}")
        return
    vmult, pvol = risk.vol_scaler(CFG)
    exposure = sum(p["market_value"] for p in positions.values())
    crypto_exp = sum(p["market_value"] for p in positions.values() if p["crypto"])
    caps_open = sum(1 for t in db.open_trades() if t.get("strategy") == "capitulation")
    for sym, (cands, dsym, gmult) in evals.items():
        if norm(sym) in positions or len(positions) >= rk["max_open_positions"]:
            continue
        ev = next((e for e in cands if e["go"]), None)
        if not ev:
            continue
        if ev["strategy"] == "capitulation" and caps_open >= CFG["capitulation"].get("max_open", 1):
            continue
        if ev["side"] == "short" and not broker.can_short(sym):
            log.info("%s non shortabile in questo momento", sym)
            continue
        price = broker.last_price(sym)
        stop = signals.initial_stop(ev, price, CFG)
        qty, why = risk.position_size(acct["equity"], price, stop, size_mult(ev, dsym, gmult, cb, vmult),
                                      CFG, exposure, crypto_exp, is_crypto(sym))
        if ev["side"] == "short" and not is_crypto(sym):
            qty = int(qty)                         # short su azioni: solo quantità intere
        if qty > 0 and hasattr(broker, "normalize_qty"):
            qty = broker.normalize_qty(sym, qty)   # precisione e minimo del contratto
            why = why if qty > 0 else "sotto la dimensione minima del contratto"
        if qty <= 0:
            log.info("%s: segnale %s ok ma size 0 (%s)", sym, ev["strategy"], why)
            continue
        fill = broker.sell_short(sym, qty) if ev["side"] == "short" else broker.buy(sym, qty)
        stop = signals.initial_stop(ev, fill, CFG)
        gap = rk["backup_stop_extra_atr"] * ev["atr_4h"] * (-1 if ev["side"] == "short" else 1)
        backup = (broker.place_backup_stop(sym, qty, stop - gap, ev["side"]) if rk.get("broker_backup_stop") else None)
        db.open_trade(symbol=sym, asset_class="crypto" if is_crypto(sym) else "stock", side=ev["side"],
                      strategy=ev["strategy"], qty=qty, entry_price=fill, stop=stop, score=ev["composite"],
                      backup_order_id=backup, rationale=ev["reason"] + (f" | Strategia: {dsym['reasoning']}" if dsym else ""))
        positions[norm(sym)] = {"market_value": qty * fill, "crypto": is_crypto(sym)}
        exposure += qty * fill
        crypto_exp += qty * fill if is_crypto(sym) else 0
        caps_open += ev["strategy"] == "capitulation"
        risk_cash = abs(fill - stop) * qty
        target = ""
        if ev["strategy"] == "capitulation":
            target = f" · obiettivo {fmt_price(fill + CFG['capitulation']['target_r'] * abs(fill - stop))}"
        db.event("trade", f"{ICON[ev['strategy']]} <b>APERTO {LABEL[ev['strategy']]} {sym}</b> {qty:g} @ {fmt_price(fill)}\n"
                          f"Stop {fmt_price(stop)}{target} (rischio {risk_cash:,.2f}$ = {risk_cash / acct['equity']:.2%})"
                          + (" · 🛡 stop emergenza sul broker" if backup else "") +
                          f"\nPunteggio {ev['composite']:.2f} · {ev['reason']}"
                          + (f"\n<i>vol portafoglio {pvol:.0%} → rischio x{vmult:.2f}</i>" if vmult < 1 else ""))
        log.info("aperto %s %s qty=%s @ %.4g", ev["strategy"], sym, qty, fill)


def sentiment_for(sym: str) -> dict:
    if not is_crypto(sym):
        return {}
    fg = macro.fear_greed()
    return {"fear_greed": fg["today"] if fg else None, "funding": macro.funding_rate(sym)}


def evaluate_universe() -> tuple[dict, dict]:
    """Scarica i dati una volta sola e valuta ogni simbolo con tutte le strategie.
    Ritorna ({sym: ([valutazioni in ordine di priorità], dsym, gmult)}, barre)."""
    evals, all_bars, stocks_open = {}, {}, None
    directive = db.latest_directive()
    now = datetime.now(timezone.utc).timestamp()
    for sym in UNIVERSE:
        if not is_crypto(sym):
            stocks_open = broker.market_open() if stocks_open is None else stocks_open
            if not stocks_open:
                continue
        try:
            bars = {k: broker.bars(sym, TF[k]) for k in ("long", "medium", "short")}
        except Exception as e:
            log.warning("dati %s: %s", sym, e)
            continue
        dsym, gmult = directive_for(sym)
        cands = [signals.evaluate_entry(sym, bars, dsym, CFG)]
        if CFG.get("shorts", {}).get("enabled") and sym in CFG["shorts"].get("symbols", []):
            cands.append(signals.evaluate_short(sym, bars, dsym, CFG))
        if CFG.get("capitulation", {}).get("enabled"):
            cap = signals.evaluate_capitulation(sym, bars, dsym, directive, sentiment_for(sym), CFG)
            if now < (db.kv_get(f"cap_cooldown:{sym}") or 0):
                cap["go"] = False
                cap["vetoes"].append("pausa dopo il rimbalzo precedente")
            cands.append(cap)
        best = next((e for e in cands if e["go"]), max(cands, key=lambda e: e["composite"]))
        db.kv_set(f"last_eval:{sym}", {"ts": db.now_iso(), "go": best["go"], "score": best["composite"],
                                       "strategy": best["strategy"], "vetoes": best["vetoes"], "reason": best["reason"],
                                       "all": {e["strategy"]: {"go": e["go"], "score": e["composite"],
                                                               "vetoes": e["vetoes"][:2]} for e in cands}})
        evals[sym], all_bars[sym] = (cands, dsym, gmult), bars
    return evals, all_bars


def run_shadows(all_bars: dict, equity: float):
    """Varianti di parametri simulate in parallelo: nessun ordine, solo diario."""
    for name, over in (CFG.get("shadow_variants") or {}).items():
        vcfg = deep_merge(CFG, over)
        rk = vcfg["risk"]
        open_syms = set()
        for t in db.shadow_open(name):
            b = all_bars.get(t["symbol"])
            if not b:
                open_syms.add(t["symbol"])
                continue
            price = float(b["short"]["close"].iloc[-1])
            atr4 = float(regime.medium_term(b["medium"])["atr"])
            lt = regime.long_term(b["long"])["label"]
            bars_since = int((datetime.now(timezone.utc) - datetime.fromisoformat(t["entry_ts"])).total_seconds() // 14400)
            reason, ns = signals.evaluate_exit(t, price, atr4, bars_since, lt, directive_for(t["symbol"])[0], vcfg)
            if reason:
                px = min(price, ns) if "stop" in reason else price
                db.shadow_update(t["id"], exit_price=px, exit_ts=db.now_iso(), exit_reason=reason, status="closed",
                                 pnl=(px - t["entry_price"]) * t["qty"],
                                 r_multiple=(px - t["entry_price"]) / max(t["entry_price"] - t["initial_stop"], 1e-9))
            else:
                db.shadow_update(t["id"], stop=ns, highest=max(t["highest"], price))
                open_syms.add(t["symbol"])
        for sym, b in all_bars.items():
            if sym in open_syms or len(open_syms) >= rk["max_open_positions"]:
                continue
            ev = signals.evaluate_entry(sym, b, directive_for(sym)[0], vcfg)  # le ombre testano il trend long
            if ev["go"]:
                px = float(b["short"]["close"].iloc[-1])
                stop = px - rk["atr_stop_mult"] * ev["atr_4h"]
                qty = min(equity * rk["risk_per_trade_pct"] / 100 / (px - stop), equity * rk["max_position_pct"] / 100 / px)
                db.shadow_insert(variant=name, symbol=sym, qty=qty, entry_price=px, entry_ts=db.now_iso(),
                                 stop=stop, initial_stop=stop, highest=px, lowest=px)
                open_syms.add(sym)


NO_CLAUDE = "senza_claude"


def run_no_claude(all_bars: dict, equity: float, cb: dict):
    """Portafoglio ombra SENZA Claude: stesse strategie, dati, rischio e blocchi del bot reale,
    ma guidato dalla direttiva a sole regole. Nessun ordine: serve a misurare quanto vale Claude."""
    rd = db.kv_get("rules_directive")
    if not rd:
        return
    rk = CFG["risk"]
    dsym_of = lambda sym: next((x for x in rd.get("symbols", []) if x["symbol"] == sym), None)
    open_syms, exposure, crypto_exp = set(), 0.0, 0.0
    now = datetime.now(timezone.utc)
    for t in db.shadow_open(NO_CLAUDE):
        b = all_bars.get(t["symbol"])
        if not b:                                  # azioni a mercato chiuso: si gestisce alla riapertura
            open_syms.add(t["symbol"])
            exposure += t["qty"] * t["entry_price"]
            continue
        price = float(b["short"]["close"].iloc[-1])
        atr4 = float(regime.medium_term(b["medium"])["atr"])
        lt = regime.long_term(b["long"])["label"]
        bars_since = int((now - datetime.fromisoformat(t["entry_ts"])).total_seconds() // 14400)
        reason, ns = signals.evaluate_exit(t, price, atr4, bars_since, lt, dsym_of(t["symbol"]), CFG)
        if reason:
            short = t.get("side") == "short"
            px = (max(price, ns) if short else min(price, ns)) if "stop" in reason else price
            sgn = -1 if short else 1
            db.shadow_update(t["id"], exit_price=px, exit_ts=db.now_iso(), exit_reason=reason, status="closed",
                             pnl=sgn * (px - t["entry_price"]) * t["qty"],
                             r_multiple=sgn * (px - t["entry_price"]) / max(abs(t["entry_price"] - t["initial_stop"]), 1e-9))
        else:
            db.shadow_update(t["id"], stop=ns, highest=max(t["highest"], price), lowest=min(t["lowest"] or price, price))
            open_syms.add(t["symbol"])
            exposure += t["qty"] * price
            crypto_exp += t["qty"] * price if is_crypto(t["symbol"]) else 0
    if db.is_paused() or cb["risk_mult"] == 0 or macro.macro_blackout(CFG):
        return                                     # stessi blocchi del bot reale
    cutoff = (now - timedelta(hours=CFG["capitulation"].get("cooldown_hours", 48))).isoformat()
    recent_caps = {r["symbol"] for r in db.shadow_closed(cutoff)                # pausa dopo un rimbalzo, come il reale
                   if r["variant"] == NO_CLAUDE and r.get("strategy") == "capitulation"}
    vmult, _ = risk.vol_scaler(CFG)
    caps_open = sum(1 for t in db.shadow_open(NO_CLAUDE) if t.get("strategy") == "capitulation")
    for sym, b in all_bars.items():
        if sym in open_syms or len(open_syms) >= rk["max_open_positions"]:
            continue
        dsym = dsym_of(sym)
        cands = [signals.evaluate_entry(sym, b, dsym, CFG)]
        if CFG.get("shorts", {}).get("enabled") and sym in CFG["shorts"].get("symbols", []):
            cands.append(signals.evaluate_short(sym, b, dsym, CFG))
        if CFG.get("capitulation", {}).get("enabled") and sym not in recent_caps:
            cands.append(signals.evaluate_capitulation(sym, b, dsym, rd, sentiment_for(sym), CFG))
        ev = next((e for e in cands if e["go"]), None)
        if not ev or (ev["strategy"] == "capitulation" and caps_open >= CFG["capitulation"].get("max_open", 1)):
            continue
        px = float(b["short"]["close"].iloc[-1])
        stop = signals.initial_stop(ev, px, CFG)
        qty, _ = risk.position_size(equity, px, stop, size_mult(ev, dsym, rd.get("global_risk_multiplier", 0.7), cb, vmult),
                                    CFG, exposure, crypto_exp, is_crypto(sym))
        if ev["side"] == "short" and not is_crypto(sym):
            qty = int(qty)
        if qty > 0 and hasattr(broker, "normalize_qty"):
            qty = broker.normalize_qty(sym, qty)
        if qty <= 0:
            continue
        db.shadow_insert(variant=NO_CLAUDE, symbol=sym, side=ev["side"], strategy=ev["strategy"], qty=qty, entry_price=px,
                         entry_ts=db.now_iso(), stop=stop, initial_stop=stop, highest=px, lowest=px)
        open_syms.add(sym)
        exposure += qty * px
        crypto_exp += qty * px if is_crypto(sym) else 0
        caps_open += ev["strategy"] == "capitulation"


def check_broker_switch():
    """Cambio di broker (es. Alpaca → conto simulato Kraken): archivia lo storico del vecchio conto
    e fa ripartire da zero confronti e statistiche, così non si mescolano conti diversi."""
    name = CFG.get("broker", "alpaca")
    prev = db.kv_get("broker_name") or ("alpaca" if db.last_equity() else None)
    if prev and prev != name:
        db.archive_account_history(f"cambio broker: {prev} → {name}")
        with db.conn() as c:
            c.execute("UPDATE shadow_trades SET status='void', exit_reason='cambio broker' WHERE status='open'")
        for k in ("ladder_start", "noclaude_start", "review_prices"):
            db.kv_set(k, db.now_iso() if k != "review_prices" else None)
        acct = broker.account()
        db.event("system", f"🔀 <b>Cambio di conto: {prev} → {name}</b>\n"
                           f"Nuovo capitale: {acct['equity']:,.2f}$. Storico del vecchio conto archiviato; confronto "
                           "con/senza Claude e scala di capitali ripartono da adesso. Diario, lezioni e costi Claude restano.")
        log.warning("cambio broker %s → %s", prev, name)
    db.kv_set("broker_name", name)


def cycle():
    try:
        check_broker_switch()
        acct = broker.account()
        positions = broker.positions()
        exposure = sum(p["market_value"] for p in positions.values())
        prev = db.last_equity()
        if prev and (acct["equity"] < 0.2 * prev or acct["equity"] > 5 * prev) and not positions:
            # salto di oltre 5 volte senza posizioni aperte: è un azzeramento/ricarica del conto, non un crollo
            db.archive_account_history(f"conto azzerato sul broker ({prev:,.0f}$ → {acct['equity']:,.2f}$)")
            db.event("system", f"🔁 <b>Conto demo azzerato</b>: {prev:,.2f}$ → {acct['equity']:,.2f}$.\n"
                               "Storico dell'equity archiviato e blocchi di sicurezza azzerati; diario, lezioni e costi "
                               "Claude restano. Riparto da qui.")
            log.warning("azzeramento del conto rilevato: %.2f → %.2f", prev, acct["equity"])
        db.snapshot_equity(acct["equity"], acct["cash"], exposure)
        cb = risk.circuit_breaker(acct["equity"], CFG)
        if cb["state"] != db.kv_get("cb_state", "ok"):
            db.kv_set("cb_state", cb["state"])
            if cb["state"] != "ok":
                db.event("risk", "🛑 <b>Circuit breaker: " + cb["state"] + "</b>\n" + "\n".join(cb["reasons"]))
        if cb["state"] == "kill":
            for t in db.open_trades():
                close_trade(t, "kill switch drawdown")
            db.kv_set("paused", True)
            return
        manage_open(positions)
        evals, all_bars = evaluate_universe()
        if not db.is_paused() and cb["risk_mult"] > 0:
            look_for_entries(acct, broker.positions(), cb, evals)
        run_shadows(all_bars, acct["equity"])
        try:   # statistiche: un loro errore non deve mai fermare il trading
            run_no_claude(all_bars, acct["equity"], cb)
        except Exception as e:
            log.warning("portafoglio senza Claude: %s", e)
        try:
            ladder.sync(acct["equity"], CFG)
        except Exception as e:
            log.warning("scala di capitali: %s", e)
        db.kv_set("heartbeat", db.now_iso())
    except Exception as e:
        log.error("errore nel ciclo: %s\n%s", e, traceback.format_exc())
        last = db.kv_get("last_error_alert", 0)
        if datetime.now(timezone.utc).timestamp() - last > 3600:  # max un alert/ora
            db.kv_set("last_error_alert", datetime.now(timezone.utc).timestamp())
            db.event("error", f"❗ Errore nel bot Trader: {type(e).__name__}: {e}")


def main():
    db.init()
    log.info("Trader avviato (paper=%s, dry_run=%s)", ENV.alpaca_paper, ENV.dry_run)
    db.event("system", f"▶️ Bot Trader avviato · conto {'DEMO' if ENV.alpaca_paper else '⚠️ REALE'}"
                       + (" · DRY RUN" if ENV.dry_run else ""))
    cycle()
    sch = BlockingScheduler(timezone=ENV.tz)
    sch.add_job(cycle, "interval", minutes=CFG["schedule"]["trader_every_minutes"], max_instances=1, coalesce=True)
    sch.start()


if __name__ == "__main__":
    main()
