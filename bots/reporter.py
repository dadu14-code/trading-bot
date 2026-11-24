"""BOT 3 — REPORTER (Telegram).

  • alert in tempo reale (aperture, chiusure, circuit breaker, errori)
  • 08:00  "Cosa sto facendo": piano del giorno, visione di mercato, posizioni
  • 21:30  "Cosa ho fatto": operazioni, P&L, equity, lezioni apprese
  • domenica: report settimanale + checklist "pronto per il conto reale"
  • comandi: /status /piano /report /settimana /lezioni /pausa /riprendi /help
"""
import html
import time
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from tradebot import db, telegram
from tradebot.broker import AlpacaBroker
from tradebot.config import ENV, load_settings, setup_logging
from tradebot.stats import go_live_readiness, max_drawdown, trade_stats

log = setup_logging("reporter")
CFG = load_settings()
broker = AlpacaBroker()
esc = lambda s: html.escape(str(s))
BIAS = {"long": "🟢 long", "flat": "⚪ flat", "exit": "🔴 uscire"}


def _since(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def positions_block() -> str:
    trades = {t["symbol"].replace("/", ""): t for t in db.open_trades()}
    pos = broker.positions()
    if not pos:
        return "Nessuna posizione aperta (liquidità al 100%)."
    lines = []
    for sym, p in pos.items():
        t = trades.get(sym)
        pct = p["price"] / p["avg_entry"] - 1
        stop = f" · stop {t['stop']:,.4g}" if t else ""
        lines.append(f"• <b>{sym}</b> {p['qty']:g} @ {p['avg_entry']:,.4g} → {p['price']:,.4g} "
                     f"({pct:+.2%}, {p['unrealized_pl']:+,.2f}$){stop}")
    return "\n".join(lines)


def account_line() -> str:
    a = broker.account()
    day = a["equity"] / a["last_equity"] - 1 if a["last_equity"] else 0
    return f"💰 Equity <b>{a['equity']:,.2f}$</b> ({day:+.2%} oggi) · cash {a['cash']:,.2f}$"


def morning_report():
    d = db.latest_directive() or {}
    lines = [f"☀️ <b>COSA STO FACENDO — {datetime.now().strftime('%d/%m/%Y')}</b>", account_line(), "",
             f"🧭 <b>Postura:</b> {esc(d.get('risk_posture', 'n/d'))} · rischio x{d.get('global_risk_multiplier', 1):.2f}"
             f" <i>({esc(d.get('_source', '-'))})</i>",
             f"<i>{esc(d.get('market_view', ''))}</i>", "", "📋 <b>Piano</b>"]
    lines += [f"• {esc(p)}" for p in d.get("plan_today", [])]
    lines += ["", "🎯 <b>Bias per asset</b>"]
    for s in d.get("symbols", []):
        lines.append(f"• <b>{esc(s['symbol'])}</b> {BIAS.get(s['bias'], s['bias'])} · conv {s['conviction']:.0%} · "
                     f"{esc(s['horizon'])}\n   <i>{esc(s['reasoning'])}</i>")
    lines += ["", "📂 <b>Posizioni</b>", positions_block()]
    if d.get("warnings"):
        lines += ["", "⚠️ " + "\n⚠️ ".join(esc(w) for w in d["warnings"])]
    if db.is_paused():
        lines.append("\n⏸ <b>Bot in PAUSA</b> (/riprendi per ripartire)")
    telegram.send("\n".join(lines))


def evening_report():
    since = _since(24)
    opened = db.trades_opened_since(since)
    closed = db.closed_trades(since_iso=since)
    st = trade_stats(closed)
    lines = [f"🌙 <b>COSA HO FATTO OGGI — {datetime.now().strftime('%d/%m/%Y')}</b>", account_line(), ""]
    lines.append(f"🔵 Aperte: {len(opened)}   ✅ Chiuse: {len(closed)}" +
                 (f"   P&L realizzato: <b>{st['pnl']:+,.2f}$</b> (win {st['win_rate']:.0%})" if closed else ""))
    for t in opened:
        lines.append(f"• ➕ {esc(t['symbol'])} @ {t['entry_price']:,.4g} — <i>{esc(t['rationale'][:220])}</i>")
    for t in closed:
        lines.append(f"• ➖ {esc(t['symbol'])} {t['pnl']:+,.2f}$ ({(t['r_multiple'] or 0):+.2f}R) — {esc(t['exit_reason'])}")
    if not opened and not closed:
        lines.append("Nessuna operazione: il mercato non offriva setup con vantaggio sufficiente. "
                     "Anche non fare nulla è una decisione.")
    # perché NON è entrato (trasparenza)
    lines += ["", "🔍 <b>Ultima valutazione per asset</b>"]
    for sym in CFG["universe"]["crypto"] + CFG["universe"]["stocks"]:
        e = db.kv_get(f"last_eval:{sym}")
        if e:
            why = "SEGNALE OK" if e["go"] else "; ".join(e["vetoes"]) or "punteggio sotto soglia"
            lines.append(f"• {sym}: {e['score']:.2f} — {esc(why)}")
    lines += ["", "📂 <b>Posizioni aperte</b>", positions_block()]
    pm = db.kv_get("last_postmortem")
    new_lessons = db.recent_lessons(3)
    if new_lessons:
        lines += ["", "🧠 <b>Ultime lezioni in memoria</b>"] + [f"• {esc(l)}" for l in new_lessons]
    eq = [e["equity"] for e in db.equity_since(_since(24 * 365))]
    lines.append(f"\n📉 Max drawdown storico: {max_drawdown(eq):.2%}")
    telegram.send("\n".join(lines))


def weekly_report():
    closed = db.closed_trades(since_iso=_since(24 * 7))
    s = trade_stats(closed)
    alltime = trade_stats(db.closed_trades(limit=10000))
    eq = db.equity_since(_since(24 * 7))
    wk = (eq[-1]["equity"] / eq[0]["equity"] - 1) if len(eq) > 1 else 0
    lines = ["📅 <b>REPORT SETTIMANALE</b>", f"Rendimento settimana: <b>{wk:+.2%}</b>"]
    if s["n"]:
        lines.append(f"Operazioni {s['n']} · win {s['win_rate']:.0%} · PF {s['profit_factor']:.2f} · "
                     f"media {s['avg_r']:+.2f}R · P&L {s['pnl']:+,.2f}$")
    if alltime["n"]:
        lines.append(f"Dall'inizio: {alltime['n']} op · win {alltime['win_rate']:.0%} · PF {alltime['profit_factor']:.2f}")
    lines += ["", "✅ <b>Checklist passaggio al conto reale</b>"]
    checks = go_live_readiness(db.closed_trades(limit=10000), db.equity_since("1970-01-01"))
    lines += [f"{'✅' if ok else '⬜'} {esc(txt)}" for txt, ok in checks]
    lines.append("\n" + ("🚀 Tutti i criteri soddisfatti: si può valutare il reale con capitale ridotto."
                         if all(ok for _, ok in checks) else "Restiamo in demo finché tutti i criteri non sono verdi."))
    telegram.send("\n".join(lines))


def flush_events():
    for e in db.pending_events():
        try:
            telegram.send(e["text"])
            db.mark_sent(e["id"])
        except Exception:
            return  # riprova al prossimo giro


def handle_command(text: str):
    cmd = text.strip().split()[0].lower().split("@")[0]
    if cmd == "/status":
        cb = db.kv_get("cb_state", "ok")
        telegram.send(f"{account_line()}\nCircuit breaker: {cb} · {'⏸ PAUSA' if db.is_paused() else '▶️ attivo'}\n\n"
                      + positions_block())
    elif cmd == "/piano":
        morning_report()
    elif cmd == "/report":
        evening_report()
    elif cmd == "/settimana":
        weekly_report()
    elif cmd == "/lezioni":
        ls = db.recent_lessons(15)
        telegram.send("🧠 <b>Lezioni in memoria</b>\n" + ("\n".join(f"• {esc(l)}" for l in ls) or "ancora nessuna"))
    elif cmd == "/pausa":
        db.kv_set("paused", True)
        telegram.send("⏸ Nuovi ingressi sospesi. Le posizioni aperte restano gestite (stop attivi).")
    elif cmd == "/riprendi":
        db.kv_set("paused", False)
        db.kv_set("cooldown_until", 0)
        telegram.send("▶️ Bot riattivato.")
    else:
        telegram.send("Comandi: /status /piano /report /settimana /lezioni /pausa /riprendi")


def main():
    db.init()
    sch = BackgroundScheduler(timezone=ENV.tz)
    h, m = CFG["schedule"]["morning_report"].split(":")
    sch.add_job(morning_report, "cron", hour=int(h), minute=int(m))
    h, m = CFG["schedule"]["evening_report"].split(":")
    sch.add_job(evening_report, "cron", hour=int(h), minute=int(m))
    sch.add_job(weekly_report, "cron", day_of_week=CFG["schedule"]["weekly_report_day"], hour=19, minute=0)
    sch.add_job(flush_events, "interval", seconds=20, max_instances=1, coalesce=True)
    sch.start()
    log.info("Reporter avviato")
    offset = None
    while True:  # long-polling dei comandi Telegram
        for u in telegram.get_updates(offset):
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != str(ENV.tg_chat):
                continue  # solo tu puoi comandare il bot
            if msg.get("text", "").startswith("/"):
                try:
                    handle_command(msg["text"])
                except Exception as e:
                    telegram.send(f"Errore: {esc(e)}")
        if not ENV.tg_token:
            time.sleep(30)


if __name__ == "__main__":
    main()
