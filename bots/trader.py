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
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

from tradebot import db, macro, regime, risk, signals
from tradebot.broker import AlpacaBroker, is_crypto, norm
from tradebot.config import ENV, require_keys, deep_merge, load_settings, setup_logging

log = setup_logging("trader")
CFG = load_settings()
require_keys(log)
broker = AlpacaBroker()
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
                      f" @ {px:,.4g}\nP&L {pnl:+,.2f}$ ({r:+.2f}R) · motivo: {reason}")
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
        if ev["side"] == "short":
            qty = int(qty)
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
            target = f" · obiettivo {fill + CFG['capitulation']['target_r'] * abs(fill - stop):,.4g}"
        db.event("trade", f"{ICON[ev['strategy']]} <b>APERTO {LABEL[ev['strategy']]} {sym}</b> {qty:g} @ {fill:,.4g}\n"
                          f"Stop {stop:,.4g}{target} (rischio {risk_cash:,.2f}$ = {risk_cash / acct['equity']:.2%})"
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


def cycle():
    try:
        acct = broker.account()
        positions = broker.positions()
        exposure = sum(p["market_value"] for p in positions.values())
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
