"""BOT 2 — TRADER (esecuzione), gira 24/7 ogni pochi minuti.

Ciclo:
  1. stato del conto + circuit breaker (kill switch se drawdown eccessivo)
  2. gestione delle posizioni aperte: trailing stop, uscite per trend/direttiva/tempo
  3. ricerca nuovi ingressi: crypto sempre, azioni/ETF solo a mercato aperto
"""
import traceback
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

from tradebot import db, regime, risk, signals
from tradebot.broker import AlpacaBroker, is_crypto, norm
from tradebot.config import ENV, load_settings, setup_logging

log = setup_logging("trader")
CFG = load_settings()
broker = AlpacaBroker()
TF = CFG["timeframes"]


def directive_for(sym: str) -> tuple[dict | None, float]:
    d = db.latest_directive()
    if not d:
        return None, 0.5
    age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(d["_ts"])).total_seconds() / 3600
    g = d.get("global_risk_multiplier", 1.0) * (0.5 if age_h > 12 else 1.0)  # direttiva vecchia → prudenza
    return next((s for s in d.get("symbols", []) if s["symbol"] == sym), None), g


def close_trade(t: dict, reason: str):
    px = broker.close(t["symbol"])
    pnl = (px - t["entry_price"]) * t["qty"]
    r = (px - t["entry_price"]) / (t["entry_price"] - t["initial_stop"]) if t["entry_price"] > t["initial_stop"] else 0
    db.update_trade(t["id"], exit_price=px, exit_ts=db.now_iso(), exit_reason=reason, pnl=pnl, r_multiple=r, status="closed")
    db.event("trade", f"{'🟢' if pnl >= 0 else '🔴'} <b>CHIUSO {t['symbol']}</b> @ {px:,.4g}\n"
                      f"P&L {pnl:+,.2f}$ ({r:+.2f}R) · motivo: {reason}")
    log.info("chiuso %s pnl=%.2f motivo=%s", t["symbol"], pnl, reason)


def manage_open(positions: dict):
    for t in db.open_trades():
        sym = t["symbol"]
        if norm(sym) not in positions:   # chiusa fuori dal bot (manuale o dal broker)
            px = broker.last_price(sym)
            db.update_trade(t["id"], exit_price=px, exit_ts=db.now_iso(), exit_reason="chiusa esternamente",
                            pnl=(px - t["entry_price"]) * t["qty"], status="closed")
            continue
        if not is_crypto(sym) and not broker.market_open():
            continue
        price = broker.last_price(sym)
        b4 = broker.bars(sym, TF["medium"])
        atr4 = float(regime.medium_term(b4)["atr"])
        lt = regime.long_term(broker.bars(sym, TF["long"]))
        bars_since = int((datetime.now(timezone.utc) - datetime.fromisoformat(t["entry_ts"])).total_seconds() // 14400)
        dsym, _ = directive_for(sym)
        reason, new_stop = signals.evaluate_exit(t, price, atr4, bars_since, lt["label"], dsym, CFG)
        db.update_trade(t["id"], highest=max(t["highest"], price), lowest=min(t["lowest"], price), stop=new_stop)
        if reason:
            close_trade({**t, "stop": new_stop}, reason)
        elif new_stop > t["stop"] * 1.002:
            log.info("%s stop alzato %.4g → %.4g", sym, t["stop"], new_stop)


def look_for_entries(acct: dict, positions: dict, cb: dict):
    rk = CFG["risk"]
    if len(positions) >= rk["max_open_positions"]:
        return
    exposure = sum(p["market_value"] for p in positions.values())
    crypto_exp = sum(p["market_value"] for p in positions.values() if p["crypto"])
    stocks_open = None
    for sym in CFG["universe"]["crypto"] + CFG["universe"]["stocks"]:
        if norm(sym) in positions:
            continue
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
        ev = signals.evaluate_entry(sym, bars, dsym, CFG)
        db.kv_set(f"last_eval:{sym}", {"ts": db.now_iso(), "go": ev["go"], "score": ev["composite"],
                                       "vetoes": ev["vetoes"], "reason": ev["reason"]})
        if not ev["go"]:
            continue
        price = broker.last_price(sym)
        stop = price - rk["atr_stop_mult"] * ev["atr_4h"]
        mult = cb["risk_mult"] * gmult * (dsym["risk_multiplier"] * (0.5 + dsym["conviction"]) if dsym else 0.5)
        qty, why = risk.position_size(acct["equity"], price, stop, mult, CFG, exposure, crypto_exp, is_crypto(sym))
        if qty <= 0:
            log.info("%s: segnale ok ma size 0 (%s)", sym, why)
            continue
        fill = broker.buy(sym, qty)
        stop = fill - rk["atr_stop_mult"] * ev["atr_4h"]
        db.open_trade(symbol=sym, asset_class="crypto" if is_crypto(sym) else "stock", side="long", qty=qty,
                      entry_price=fill, stop=stop, score=ev["composite"],
                      rationale=ev["reason"] + (f" | Strategia: {dsym['reasoning']}" if dsym else ""))
        exposure += qty * fill
        crypto_exp += qty * fill if is_crypto(sym) else 0
        risk_cash = (fill - stop) * qty
        db.event("trade", f"🔵 <b>APERTO {sym}</b> {qty:g} @ {fill:,.4g}\n"
                          f"Stop {stop:,.4g} (rischio {risk_cash:,.2f}$ = {risk_cash / acct['equity']:.2%})\n"
                          f"Punteggio {ev['composite']:.2f} · {ev['reason']}")
        log.info("aperto %s qty=%s @ %.4g", sym, qty, fill)


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
        if db.is_paused() or cb["risk_mult"] == 0:
            return
        look_for_entries(acct, broker.positions(), cb)
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
