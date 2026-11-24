"""BOT 1 — STRATEGIA ("il trader senior").

Ogni N ore: legge il regime di mercato su 3 orizzonti, le analogie storiche,
le posizioni, il diario e le lezioni → chiede a Claude una direttiva → la salva.
Ogni mattina: postmortem delle operazioni chiuse → nuove lezioni in memoria.
"""
from apscheduler.schedulers.blocking import BlockingScheduler

from tradebot import db, macro, patterns, regime, risk
from tradebot.broker import AlpacaBroker
from tradebot.config import ENV, require_keys, load_settings, setup_logging
from tradebot.llm import SeniorTrader

log = setup_logging("strategist")
CFG = load_settings()
require_keys(log)
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
                        "price_time_utc": b["short"].index[-1].isoformat(),
                        "price_note": "ultimo prezzo orario; può differire da last_daily_close (gap di apertura, barra giornaliera di ieri)",
                        "change_24h": float(b["short"]["close"].iloc[-1] / b["short"]["close"].iloc[-25] - 1),
                        "long_term_1D": regime.long_term(b["long"]), "medium_term_4H": m,
                        "short_term_1H": regime.short_term_trigger(b["short"]),
                        "historical_analogs": an.as_dict() if an else None,
                        "candles_1H": patterns.candle_patterns(b["short"]),
                        "price_levels": regime.price_levels(b["long"], b["medium"]),
                        "capitulation_check": {k: v for k, v in regime.capitulation(b["long"], b["medium"], b["short"]).items()
                                               if k not in ("atr_4h",)}}
        except Exception as e:  # un simbolo senza dati non deve fermare tutto
            log.warning("dati %s non disponibili: %s", sym, e)
    return out


def rule_based_directive(snap: dict) -> dict:
    """Ripiego senza LLM: regole semplici e prudenti."""
    syms = []
    shortable = CFG.get("shorts", {}).get("symbols", []) if CFG.get("shorts", {}).get("enabled") else []
    for sym, s in snap.items():
        lt, mt = s["long_term_1D"], s["medium_term_4H"]
        if lt["label"] == "BEAR":
            bias = "short" if (sym in shortable and mt["score"] < -0.3) else "flat"
        else:
            bias = "long" if (lt["label"] == "BULL" or mt["score"] > 0.3) else "flat"
        syms.append({"symbol": sym, "bias": bias, "conviction": 0.5, "risk_multiplier": 1.0 if lt["label"] == "BULL" else 0.5,
                     "horizon": "swing", "key_levels": "", "reasoning": f"regola: 1D {lt['label']}, 4H {mt['score']:+.2f}"})
    return {"market_view": "Direttiva automatica a regole (Claude non disponibile).", "risk_posture": "defensive",
            "global_risk_multiplier": 0.7, "capitulation_allowed": True, "symbols": syms, "plan_today": ["Seguire i segnali quantitativi con rischio ridotto."],
            "warnings": ["LLM non disponibile: uso regole di riserva"]}


def review():
    log.info("revisione strategica…")
    snap = market_snapshot()
    acct = broker.account()
    cb = risk.circuit_breaker(acct["equity"], CFG)
    ctx = {"account": acct, "circuit_breaker": cb, "positions": broker.positions(),
           "open_trades": db.open_trades(), "recent_closed_trades": db.closed_trades(limit=15),
           "macro_and_sentiment": macro.snapshot(CFG),
           "portfolio_vol_7d": risk.vol_scaler(CFG)[1],
           "lessons_from_past": db.recent_lessons(25), "markets": snap,
           "previous_directive": {k: v for k, v in (db.latest_directive() or {}).items() if k in ("market_view", "risk_posture")},
           "risk_limits": CFG["risk"], "shorts": CFG.get("shorts"), "capitulation": CFG.get("capitulation")}
    log.info("dati raccolti per %d asset, chiedo a Claude (%s)…", len(snap), "attivo" if senior.enabled else "chiave assente")
    d = senior.strategy_review(ctx)
    src = "claude"
    if not d:
        d, src = rule_based_directive(snap), "rules"
    # direttiva "senza Claude" (gratuita): guida il portafoglio ombra di confronto
    db.kv_set("rules_directive", {**rule_based_directive(snap), "_ts": db.now_iso()})
    if not db.kv_get("noclaude_start"):
        db.kv_set("noclaude_start", db.now_iso())
    prev = db.latest_directive()
    db.save_directive(d, src)
    db.kv_set("review_prices", {k: v["price"] for k, v in snap.items()})
    log.info("direttiva salvata (%s): %s, rischio x%.2f", src, d["risk_posture"], d["global_risk_multiplier"])
    if not prev or prev.get("risk_posture") != d["risk_posture"]:
        db.event("strategy", f"🧭 <b>Cambio postura</b>: {prev.get('risk_posture') if prev else '—'} → "
                             f"<b>{d['risk_posture']}</b>\n{d['market_view']}")
    old = set((prev or {}).get("warnings", []))
    new = [w for w in d.get("warnings", []) if w not in old]
    if new:
        db.event("warning", "⚠️ <b>Avvisi del bot Strategia</b>\n" + "\n".join(f"• {w}" for w in new))


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


def safe(job):
    """Un errore in un'analisi non deve mai spegnere il bot: avvisa e riprova al giro dopo."""
    def run():
        try:
            job()
        except Exception as e:
            log.exception("errore in %s", job.__name__)
            db.event("error", f"❗ Bot Strategia, errore in {job.__name__}: {type(e).__name__}: {str(e)[:300]}\n"
                              "Riprovo tra 15 minuti.")
            db.kv_set("strategist_retry", True)
    run.__name__ = job.__name__
    return run


def big_move_check():
    """Rivaluta subito se un asset si è mosso più della soglia dall'ultima analisi
    (max una rivalutazione straordinaria all'ora)."""
    import time
    ref = db.kv_get("review_prices") or {}
    thr = CFG["schedule"].get("rereview_move_pct", 3.0) / 100
    if not ref or time.time() - db.kv_get("last_extra_review", 0) < 3600:
        return
    moved = []
    for sym, p0 in ref.items():
        try:
            p1 = float(broker.bars(sym, CFG["timeframes"]["short"])["close"].iloc[-1])
        except Exception:
            continue
        if abs(p1 / p0 - 1) >= thr:
            moved.append(f"{sym} {p1 / p0 - 1:+.1%}")
    if moved:
        db.kv_set("last_extra_review", time.time())
        db.event("strategy", "⚡ Movimento forte (" + ", ".join(moved) + "): rivaluto la strategia ora.")
        safe(review)()


def retry_if_failed():
    if db.kv_get("strategist_retry") or not db.latest_directive():
        db.kv_set("strategist_retry", False)
        safe(review)()


def main():
    db.init()
    db.event("system", "🧭 Bot Strategia avviato: prima analisi in corso (1–3 minuti)…")
    safe(review)()
    sch = BlockingScheduler(timezone=ENV.tz)
    sch.add_job(retry_if_failed, "interval", minutes=15, id="retry", max_instances=1, coalesce=True)
    sch.add_job(safe(big_move_check), "interval", minutes=15, id="bigmove", max_instances=1, coalesce=True)
    sch.add_job(safe(review), "interval", hours=CFG["schedule"]["strategist_every_hours"], id="review",
                max_instances=1, coalesce=True)
    sch.add_job(safe(postmortem), "cron", hour=7, minute=15, id="postmortem")
    sch.add_job(safe(review), "cron", hour=7, minute=40, id="morning_review")  # pronta per il report delle 08:00
    sch.start()


if __name__ == "__main__":
    main()
