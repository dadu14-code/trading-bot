"""BOT 5 — OPTIMIZER: ottimizzazione e verifica settimanale su dati reali.

Ogni settimana (e su richiesta con /ottimizza):
  • TREND LONG: walk-forward sui 2 anni di dati orari di tutti gli asset → parametri
    suggeriti misurati SOLO su periodi mai visti, confrontati con quelli attuali
  • SHORT ETF e RIMBALZO: backtest con i parametri attuali (verifica che reggano)
Il risultato arriva su Telegram. Nessun parametro viene cambiato automaticamente.
"""
import json
import os
import time
import traceback
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

from tradebot import backtest, db, optimize
from tradebot.config import ENV, load_settings, require_keys, setup_logging

log = setup_logging("optimizer")
CFG = load_settings()
require_keys(log)
OUT = Path(ENV.db_path).parent / "optimization"


def fmt(s: dict) -> str:
    if not s.get("n"):
        return "nessuna operazione"
    return f"{s['n']} op · win {s['win_rate']:.0%} · {s['avg_r']:+.2f}R medio · PF {s['profit_factor']:.2f}"


def total_r(s: dict) -> float:
    return s.get("avg_r", 0) * s.get("n", 0)


def run():
    oc = CFG.get("optimization", {})
    days, folds = oc.get("history_days", 730), oc.get("folds", 4)
    t_start = time.time()
    db.kv_set("optimization_running", True)
    db.event("system", f"🧪 Ottimizzazione avviata: {days} giorni di dati orari, ci vorrà un po'…")
    universe = CFG["universe"]["crypto"] + CFG["universe"]["stocks"]
    data, pres, missing = {}, [], []
    for sym in universe:
        try:
            df = backtest.load(sym, days)
            if len(df) < 2500:
                raise ValueError(f"storia troppo corta ({len(df)} barre)")
            data[sym] = df
            fee = backtest.FEES["crypto" if "/" in sym else "stock"]
            log.info("precalcolo %s (%d barre)", sym, len(df))
            pres.append((backtest.precompute(df, CFG), fee))
        except Exception as e:
            log.warning("salto %s: %s", sym, e)
            missing.append(f"{sym} ({e})")
    if not pres:
        raise RuntimeError("nessun dato storico disponibile")

    base = backtest.params_from(CFG)
    wf = optimize.walk_forward(pres, base, folds)
    cur, oos = wf["current_params_same_period"], wf["oos_total"]
    gain = total_r(oos) - total_r(cur)
    recommend = (gain >= oc.get("min_gain_r", 3.0) and oos.get("n", 0) >= oc.get("min_trades", 20)
                 and oos.get("profit_factor", 0) >= 1.1)
    changed = {k: (base[k], v) for k, v in wf["suggested"].items() if base.get(k) != v}

    modules = {}
    for strat, syms in (("short", [s for s in CFG.get("shorts", {}).get("symbols", []) if s in data]),
                        ("capitulation", list(data))):
        trades = []
        for sym in syms:
            fee = backtest.FEES["crypto" if "/" in sym else "stock"]
            trades += backtest.run_module(data[sym], CFG, strat, fee=fee)["trades"]
        modules[strat] = backtest.trade_stats(trades)

    res = {"ts": db.now_iso(), "days": days, "period": wf["period"], "assets": list(data), "missing": missing,
           "current": cur, "walk_forward": oos, "suggested": wf["suggested"], "changed": changed,
           "gain_r": round(gain, 2), "recommend": recommend, "folds": wf["folds"], "modules": modules,
           "minutes": round((time.time() - t_start) / 60, 1)}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{datetime.now():%Y-%m-%d}.json").write_text(json.dumps(res, indent=2, default=str))
    db.kv_set("last_optimization", res)

    lines = [f"🧪 <b>OTTIMIZZAZIONE SETTIMANALE</b> ({len(data)} asset, fuori campione {wf['period'][0]} → {wf['period'][1]})", "",
             "🔵 <b>Trend long</b> – solo periodi mai visti in ottimizzazione:",
             f"• parametri attuali: {fmt(cur)}",
             f"• walk-forward: {fmt(oos)}"]
    if changed:
        lines.append("• suggeriti: " + ", ".join(f"{k} {a}→{b}" for k, (a, b) in changed.items()))
    if recommend:
        lines.append(f"✅ <b>Consiglio di applicarli</b>: +{gain:.1f}R fuori campione. Scrivi a Claude \"applica i parametri ottimizzati\".")
    elif not changed:
        lines.append("➖ I parametri attuali sono già i migliori: nessuna modifica.")
    else:
        lines.append(f"➖ <b>Lascio quelli attuali</b>: differenza {gain:+.1f}R, non abbastanza solida.")
    lines += ["", "🟣 <b>Short ETF</b> (parametri attuali): " + fmt(modules["short"]),
              "🟡 <b>Rimbalzo</b> (parametri attuali, senza dati storici di sentiment): " + fmt(modules["capitulation"])]
    if missing:
        lines.append("\n⚠️ Esclusi: " + "; ".join(missing))
    lines.append(f"\n<i>Durata {res['minutes']} min. Nessun parametro è stato cambiato.</i>")
    db.event("optimization", "\n".join(lines))
    log.info("ottimizzazione completata in %.1f min", res["minutes"])


def safe_run():
    try:
        run()
    except Exception as e:
        log.error("ottimizzazione fallita: %s\n%s", e, traceback.format_exc())
        db.event("error", f"❗ Ottimizzazione settimanale fallita: {type(e).__name__}: {str(e)[:300]}")
    finally:
        db.kv_set("optimization_running", False)
        db.kv_set("optimize_requested", False)


def check_request():
    if db.kv_get("optimize_requested"):
        safe_run()


def main():
    try:
        os.nice(10)   # calcolo pesante: priorità bassa, i bot di trading vengono prima
    except (AttributeError, OSError):
        pass
    db.init()
    db.kv_set("optimization_running", False)
    if not db.kv_get("last_optimization"):
        db.kv_set("optimize_requested", True)   # prima volta: parte da sola dopo 10 minuti
    oc = CFG.get("optimization", {})
    sch = BlockingScheduler(timezone=ENV.tz)
    sch.add_job(safe_run, "cron", day_of_week=oc.get("day", "sun"), hour=oc.get("hour", 4), minute=0,
                max_instances=1, coalesce=True)
    sch.add_job(check_request, "interval", minutes=10, max_instances=1, coalesce=True)
    log.info("Optimizer avviato")
    sch.start()


if __name__ == "__main__":
    main()
