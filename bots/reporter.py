"""BOT 3 — REPORTER (Telegram).

  • alert in tempo reale (aperture, chiusure, circuit breaker, errori)
  • 08:00  "Cosa sto facendo": piano del giorno, visione di mercato, posizioni
  • 21:30  "Cosa ho fatto": operazioni, P&L, equity, lezioni apprese
  • domenica: report settimanale + checklist "pronto per il conto reale"
  • watchdog: avvisa se il Trader smette di girare (PC spento, crash)
  • comandi: /status /piano /report /costi /settimana /lezioni /ottimizza /check /pausa /riprendi
"""
import html
import time
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from tradebot import db, macro, telegram
from tradebot.broker import AlpacaBroker
from tradebot.config import ENV, require_keys, load_settings, setup_logging
from tradebot.stats import go_live_readiness, max_drawdown, trade_stats
from tradebot.check import run_checks

log = setup_logging("reporter")
CFG = load_settings()
require_keys(log)
broker = AlpacaBroker()
esc = lambda s: html.escape(str(s))
GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
BIAS = {"long": "🟢 long", "short": "🟣 short", "flat": "⚪ flat", "exit": "🔴 uscire"}
STRAT = {"trend": "trend", "short": "short", "capitulation": "rimbalzo"}


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
        short = p.get("side") == "short" or p["qty"] < 0
        pct = (p["price"] / p["avg_entry"] - 1) * (-1 if short else 1)
        stop = f" · stop {t['stop']:,.4g}" if t else ""
        tag = f" [{STRAT.get(t.get('strategy') or 'trend', '')}]" if t else ""
        lines.append(f"• <b>{sym}</b> {'SHORT' if short else 'LONG'}{tag} {abs(p['qty']):g} @ {p['avg_entry']:,.4g} → "
                     f"{p['price']:,.4g} ({pct:+.2%}, {p['unrealized_pl']:+,.2f}$){stop}")
    return "\n".join(lines)


def account_line() -> str:
    a = broker.account()
    day = a["equity"] / a["last_equity"] - 1 if a["last_equity"] else 0
    return f"💰 Equity <b>{a['equity']:,.2f}$</b> ({day:+.2%} oggi) · cash {a['cash']:,.2f}$"


def result_block(hours: int, title: str) -> list[str]:
    """Guadagno del conto contro costo di Claude, con proiezione sul futuro conto reale."""
    rp = CFG.get("report", {})
    cap, cur, fx = rp.get("real_capital", 100), rp.get("real_currency", "EUR"), rp.get("usd_per_eur", 1.08)
    sym = "€" if cur == "EUR" else cur
    since = _since(hours)
    closed = db.closed_trades(since_iso=since, limit=10000)
    realized = sum(t["pnl"] or 0 for t in closed)
    eq_now = broker.account()["equity"]
    rows = db.equity_since(since)
    eq_start = rows[0]["equity"] if rows else eq_now
    change = eq_now - eq_start
    u = db.llm_usage_since(since)
    first = db.equity_since("1970")
    eq0 = first[0]["equity"] if first else eq_now
    u_all = db.llm_usage_since("1970")
    tot_change = eq_now - eq0
    lines = [f"📊 <b>{title}</b>",
             f"• Posizioni chiuse: {len(closed)} · P&L realizzato <b>{realized:+,.2f}$</b>"]
    for t in closed:
        lines.append(f"   – {esc(t['symbol'])} [{STRAT.get(t.get('strategy') or 'trend')}] {t['pnl']:+,.2f}$ "
                     f"({(t['r_multiple'] or 0):+.2f}R) · {esc(t['exit_reason'])}")
    lines += [f"• Variazione del conto (incluse posizioni aperte): <b>{change:+,.2f}$</b> ({change / eq_start:+.2%})",
              f"• Costo Claude: <b>{u['cost_usd']:.2f}$</b> · {u['calls']} analisi · "
              f"{(u['input_tokens'] + u['output_tokens']) / 1000:,.0f}k token "
              f"({u['input_tokens'] / 1000:,.0f}k in, {u['output_tokens'] / 1000:,.0f}k out)",
              f"• <b>Netto: {change - u['cost_usd']:+,.2f}$</b> (variazione conto − Claude)",
              f"• Dall'inizio: conto {tot_change:+,.2f}$ ({tot_change / eq0:+.2%}) − Claude {u_all['cost_usd']:.2f}$ "
              f"= <b>{tot_change - u_all['cost_usd']:+,.2f}$</b>"]
    # proiezione: stesso rendimento % sul capitale reale, ma costo Claude identico (non scala col capitale)
    g_day, c_day = cap * change / eq_start, u["cost_usd"] / fx
    g_all, c_all = cap * tot_change / eq0, u_all["cost_usd"] / fx
    lines += [f"• 🔮 Su un conto reale da {cap:,.0f}{sym}: {g_day:+.2f}{sym} − Claude {c_day:.2f}{sym} = "
              f"<b>{g_day - c_day:+.2f}{sym}</b> · dall'inizio {g_all - c_all:+.2f}{sym}"]
    return lines


def morning_report():
    d = db.latest_directive() or {}
    if not d:
        telegram.send("☀️ <b>COSA STO FACENDO</b>\n" + account_line() + "\n\n⏳ La prima analisi strategica non è "
                      "ancora pronta (serve qualche minuto dall'avvio). Se dura più di 15 minuti, controlla i log:\n"
                      "<code>docker compose logs --tail 40 strategist</code>\n\n📂 <b>Posizioni</b>\n" + positions_block())
        return
    lines = [f"☀️ <b>COSA STO FACENDO — {datetime.now().strftime('%d/%m/%Y')}</b>", account_line(), "",
             f"🧭 <b>Postura:</b> {esc(d.get('risk_posture', 'n/d'))} · rischio x{d.get('global_risk_multiplier', 1):.2f}"
             f" <i>({esc(d.get('_source', '-'))})</i>",
             f"<i>{esc(d.get('market_view', ''))}</i>", "", "📋 <b>Piano</b>"]
    lines += [f"• {esc(p)}" for p in d.get("plan_today", [])]
    if "capitulation_allowed" in d:
        lines.append(f"🟡 Rimbalzi da capitolazione: {'consentiti' if d['capitulation_allowed'] else 'vietati'}")
    lines += ["", "🎯 <b>Bias per asset</b>"]
    for s in d.get("symbols", []):
        lines.append(f"• <b>{esc(s['symbol'])}</b> {BIAS.get(s['bias'], s['bias'])} · conv {s['conviction']:.0%} · "
                     f"{esc(s['horizon'])}\n   <i>{esc(s['reasoning'])}</i>")
    lines += ["", "📂 <b>Posizioni</b>", positions_block()]
    lines += events_block()
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
    lines += result_block(24, "RISULTATO DELLA GIORNATA") + [""]
    lines.append(f"🔵 Aperte: {len(opened)}   ✅ Chiuse: {len(closed)}" +
                 (f"   P&L realizzato: <b>{st['pnl']:+,.2f}$</b> (win {st['win_rate']:.0%})" if closed else ""))
    for t in opened:
        lines.append(f"• ➕ {esc(t['symbol'])} {(t.get('side') or 'long').upper()} [{STRAT.get(t.get('strategy') or 'trend')}] "
                     f"@ {t['entry_price']:,.4g} — <i>{esc(t['rationale'][:220])}</i>")
    for t in closed:
        lines.append(f"• ➖ {esc(t['symbol'])} [{STRAT.get(t.get('strategy') or 'trend')}] {t['pnl']:+,.2f}$ "
                     f"({(t['r_multiple'] or 0):+.2f}R) — {esc(t['exit_reason'])}")
    if not opened and not closed:
        lines.append("Nessuna operazione: il mercato non offriva setup con vantaggio sufficiente. "
                     "Anche non fare nulla è una decisione.")
    # perché NON è entrato (trasparenza)
    lines += ["", "🔍 <b>Ultima valutazione per asset</b>"]
    for sym in CFG["universe"]["crypto"] + CFG["universe"]["stocks"]:
        e = db.kv_get(f"last_eval:{sym}")
        if e:
            parts = []
            for strat, v in (e.get("all") or {e.get("strategy", "trend"): e}).items():
                why = "✅ SEGNALE" if v["go"] else ("; ".join(v["vetoes"][:1]) or "sotto soglia")
                parts.append(f"{STRAT.get(strat, strat)} {v['score']:.2f} ({esc(why)})")
            lines.append(f"• <b>{sym}</b>: " + " · ".join(parts))
    lines += ["", "📂 <b>Posizioni aperte</b>", positions_block()]
    pm = db.kv_get("last_postmortem")
    new_lessons = db.recent_lessons(3)
    if new_lessons:
        lines += ["", "🧠 <b>Ultime lezioni in memoria</b>"] + [f"• {esc(l)}" for l in new_lessons]
    eq = [e["equity"] for e in db.equity_since(_since(24 * 365))]
    lines.append(f"\n📉 Max drawdown storico: {max_drawdown(eq):.2%}")
    lines += events_block()
    telegram.send("\n".join(lines))


def weekly_report():
    closed = db.closed_trades(since_iso=_since(24 * 7))
    s = trade_stats(closed)
    alltime = trade_stats(db.closed_trades(limit=10000))
    eq = db.equity_since(_since(24 * 7))
    wk = (eq[-1]["equity"] / eq[0]["equity"] - 1) if len(eq) > 1 else 0
    lines = ["📅 <b>REPORT SETTIMANALE</b>", f"Rendimento settimana: <b>{wk:+.2%}</b>", ""]
    lines += result_block(24 * 7, "RISULTATO DELLA SETTIMANA") + [""]
    if s["n"]:
        lines.append(f"Operazioni {s['n']} · win {s['win_rate']:.0%} · PF {s['profit_factor']:.2f} · "
                     f"media {s['avg_r']:+.2f}R · P&L {s['pnl']:+,.2f}$")
    if alltime["n"]:
        lines.append(f"Dall'inizio: {alltime['n']} op · win {alltime['win_rate']:.0%} · PF {alltime['profit_factor']:.2f}")
        everything = db.closed_trades(limit=10000)
        for strat in ("trend", "short", "capitulation"):
            st = trade_stats([t for t in everything if (t.get("strategy") or "trend") == strat])
            if st["n"]:
                lines.append(f"  · {STRAT[strat]}: {st['n']} op · win {st['win_rate']:.0%} · {st['avg_r']:+.2f}R medio · "
                             f"PF {st['profit_factor']:.2f}")
    lines += shadow_block(_since(24 * 7)) + shadow_block("1970", "dall'inizio")
    opt = db.kv_get("last_optimization")
    if opt:
        verdict = "✅ consiglio di applicare i parametri suggeriti" if opt["recommend"] else "➖ parametri attuali confermati"
        lines += ["", f"🧪 <b>Ultima ottimizzazione</b> ({opt['ts'][:10]}): trend fuori campione "
                      f"{opt['walk_forward'].get('n', 0)} op, {opt['walk_forward'].get('avg_r', 0):+.2f}R medio · {verdict}"]
    lines += ["", "✅ <b>Checklist passaggio al conto reale</b>"]
    checks = go_live_readiness(db.closed_trades(limit=10000), db.equity_since("1970-01-01"))
    lines += [f"{'✅' if ok else '⬜'} {esc(txt)}" for txt, ok in checks]
    lines.append("\n" + ("🚀 Tutti i criteri soddisfatti: si può valutare il reale con capitale ridotto."
                         if all(ok for _, ok in checks) else "Restiamo in demo finché tutti i criteri non sono verdi."))
    telegram.send("\n".join(lines))


def events_block() -> list[str]:
    ev = macro.upcoming_events(36, CFG)
    if not ev:
        return []
    return ["", "📅 <b>Eventi macro in arrivo</b> (ingressi in pausa a ridosso)"] + [
        f"• {GIORNI[e['ts'].astimezone().weekday()]} {e['ts'].astimezone():%d %H:%M} {esc(e['country'])} {esc(e['title'])}" for e in ev]


def shadow_block(since: str, label: str = "settimana") -> list[str]:
    rows = db.shadow_closed(since)
    if not rows:
        return []
    real = trade_stats(db.closed_trades(since_iso=since, limit=10000))
    out = ["", f"🧪 <b>Strategie ombra vs reale ({label})</b>",
           f"• <b>reale</b>: {real.get('n', 0)} op · " + (f"{real['avg_r']:+.2f}R medio · PF {real['profit_factor']:.2f}" if real.get("n") else "—")]
    for v in sorted({r["variant"] for r in rows}):
        st = trade_stats([r for r in rows if r["variant"] == v])
        out.append(f"• {esc(v)}: {st['n']} op · {st['avg_r']:+.2f}R medio · PF {st['profit_factor']:.2f}")
    return out


def watchdog():
    """Se il Trader non dà segni di vita da 20 minuti (PC spento, crash, rete) avvisa."""
    hb = db.kv_get("heartbeat")
    if not hb:
        return
    late = (datetime.now(timezone.utc) - datetime.fromisoformat(hb)).total_seconds() / 60
    if late > 20 and not db.kv_get("watchdog_alerted"):
        db.kv_set("watchdog_alerted", True)
        telegram.send(f"🚨 <b>Il bot Trader non risponde da {late:.0f} minuti.</b>\n"
                      "Controlla che il PC/VPS sia acceso e i bot attivi. Gli stop di emergenza sul broker restano attivi.")
    elif late <= 20 and db.kv_get("watchdog_alerted"):
        db.kv_set("watchdog_alerted", False)
        telegram.send("✅ Bot Trader di nuovo operativo.")


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
    elif cmd == "/costi":
        telegram.send("\n".join(result_block(24, "ULTIME 24 ORE") + [""] + result_block(24 * 7, "ULTIMI 7 GIORNI")))
    elif cmd == "/ottimizza":
        if db.kv_get("optimization_running"):
            telegram.send("🧪 Un'ottimizzazione è già in corso: ti scrivo quando finisce.")
        else:
            db.kv_set("optimize_requested", True)
            telegram.send("🧪 Ottimizzazione richiesta: parte entro 10 minuti e dura un po'. Ti scrivo il risultato.")
    elif cmd == "/check":
        telegram.send("🩺 <b>Diagnostica</b>\n" + "\n".join(f"{'✅' if ok else '❌'} {esc(n)}: {esc(d)}" for n, ok, d in run_checks()))
    elif cmd == "/pausa":
        db.kv_set("paused", True)
        telegram.send("⏸ Nuovi ingressi sospesi. Le posizioni aperte restano gestite (stop attivi).")
    elif cmd == "/riprendi":
        db.kv_set("paused", False)
        db.kv_set("cooldown_until", 0)
        telegram.send("▶️ Bot riattivato.")
    else:
        telegram.send("Comandi: /status /piano /report /costi /settimana /lezioni /ottimizza /check /pausa /riprendi")


def main():
    db.init()
    sch = BackgroundScheduler(timezone=ENV.tz)
    h, m = CFG["schedule"]["morning_report"].split(":")
    sch.add_job(morning_report, "cron", hour=int(h), minute=int(m))
    h, m = CFG["schedule"]["evening_report"].split(":")
    sch.add_job(evening_report, "cron", hour=int(h), minute=int(m))
    sch.add_job(weekly_report, "cron", day_of_week=CFG["schedule"]["weekly_report_day"], hour=19, minute=0)
    sch.add_job(flush_events, "interval", seconds=20, max_instances=1, coalesce=True)
    sch.add_job(watchdog, "interval", minutes=5, max_instances=1, coalesce=True)
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
