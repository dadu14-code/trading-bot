"""BOT 1 — STRATEGIA ("il trader senior").

Ogni N ore: legge il regime di mercato su 3 orizzonti, le analogie storiche,
le posizioni, il diario e le lezioni → chiede a Claude una direttiva → la salva.
Ogni mattina: postmortem delle operazioni chiuse → nuove lezioni in memoria.
"""
from apscheduler.schedulers.blocking import BlockingScheduler

from tradebot import db, patterns, regime, risk
from tradebot.broker import AlpacaBroker
from tradebot.config import ENV, load_settings, setup_logging
from tradebot.llm import SeniorTrader

log = setup_logging("strategist")
CFG = load_settings()
broker = AlpacaBroker()
senior = SeniorTrader()


def market_snapshot() -> dict:
    out = {}
    tf = CFG["timeframes"]
    for sym in CFG["universe"]["crypto"] + CFG["universe"]["stocks"]:
        try:
            b = {k: broker.bars(sym, tf[k]) for k in ("long", "medium", "short")}
            an = patterns.analogs(b["short"], k=CFG["signals"]["analog_k"], horizon=CFG["signals"]["analog_horizon_bars"])
            m = regime.medium_term(b["medium"])
            m.pop("atr", None)
            out[sym] = {"price": float(b["short"]["close"].iloc[-1]),
                        "change_24h": float(b["short"]["close"].iloc[-1] / b["short"]["close"].iloc[-25] - 1),
                        "long_term_1D": regime.long_term(b["long"]), "medium_term_4H": m,
                        "short_term_1H": regime.short_term_trigger(b["short"]),
                        "historical_analogs": an.as_dict() if an else None,
                        "candles_1H": patterns.candle_patterns(b["short"])}
        except Exception as e:  # un simbolo senza dati non deve fermare tutto
            log.warning("dati %s non disponibili: %s", sym, e)
    return out


def rule_based_directive(snap: dict) -> dict:
    """Ripiego senza LLM: regole semplici e prudenti."""
    syms = []
    for sym, s in snap.items():
        lt, mt = s["long_term_1D"], s["medium_term_4H"]
        bias = "exit" if lt["label"] == "BEAR" else "long" if (lt["label"] == "BULL" or mt["score"] > 0.3) else "flat"
        syms.append({"symbol": sym, "bias": bias, "conviction": 0.5, "risk_multiplier": 1.0 if lt["label"] == "BULL" else 0.5,
                     "horizon": "swing", "key_levels": "", "reasoning": f"regola: 1D {lt['label']}, 4H {mt['score']:+.2f}"})
    return {"market_view": "Direttiva automatica a regole (Claude non disponibile).", "risk_posture": "defensive",
            "global_risk_multiplier": 0.7, "symbols": syms, "plan_today": ["Seguire i segnali quantitativi con rischio ridotto."],
            "warnings": ["LLM non disponibile: uso regole di riserva"]}


def review():
    log.info("revisione strategica…")
    snap = market_snapshot()
    acct = broker.account()
    cb = risk.circuit_breaker(acct["equity"], CFG)
    ctx = {"account": acct, "circuit_breaker": cb, "positions": broker.positions(),
           "open_trades": db.open_trades(), "recent_closed_trades": db.closed_trades(limit=15),
           "lessons_from_past": db.recent_lessons(25), "markets": snap,
           "previous_directive": {k: v for k, v in (db.latest_directive() or {}).items() if k in ("market_view", "risk_posture")},
           "risk_limits": CFG["risk"]}
    d = senior.strategy_review(ctx)
    src = "claude"
    if not d:
        d, src = rule_based_directive(snap), "rules"
    prev = db.latest_directive()
    db.save_directive(d, src)
    log.info("direttiva salvata (%s): %s, rischio x%.2f", src, d["risk_posture"], d["global_risk_multiplier"])
    if not prev or prev.get("risk_posture") != d["risk_posture"]:
        db.event("strategy", f"🧭 <b>Cambio postura</b>: {prev.get('risk_posture') if prev else '—'} → "
                             f"<b>{d['risk_posture']}</b>\n{d['market_view']}")
    for w in d.get("warnings", []):
        db.event("warning", f"⚠️ {w}")


def postmortem():
    todo = [t for t in db.closed_trades(limit=50) if not t["postmortem_done"]]
    if not todo:
        return
    res = senior.postmortem(todo, db.recent_lessons(40), {"note": "contesto attuale", **{
        s: v["long_term_1D"] for s, v in market_snapshot().items()}})
    if res:
        for l in res["lessons"]:
            db.add_lesson(l)
        for s in res["parameter_suggestions"]:
            db.event("idea", f"💡 Suggerimento parametri: {s}")
        db.kv_set("last_postmortem", res)
    for t in todo:
        db.update_trade(t["id"], postmortem_done=1)
    log.info("postmortem su %d operazioni", len(todo))


def main():
    db.init()
    review()
    sch = BlockingScheduler(timezone=ENV.tz)
    sch.add_job(review, "interval", hours=CFG["schedule"]["strategist_every_hours"], id="review",
                max_instances=1, coalesce=True)
    sch.add_job(postmortem, "cron", hour=7, minute=15, id="postmortem")
    sch.add_job(review, "cron", hour=7, minute=40, id="morning_review")  # pronta per il report delle 08:00
    sch.start()


if __name__ == "__main__":
    main()
