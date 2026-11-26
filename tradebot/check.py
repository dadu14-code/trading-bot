"""Diagnostica dei collegamenti. Uso:  python -m tradebot.check  [--telegram-test]"""
import sys

from .config import ENV, load_settings


def _try(name, fn):
    try:
        return name, True, fn()
    except Exception as e:
        return name, False, f"{type(e).__name__}: {str(e)[:160]}"


def run_checks(telegram_test: bool = False) -> list[tuple[str, bool, str]]:
    cfg = load_settings()
    out = []

    bname = cfg.get("broker", "alpaca")
    label = {"alpaca": "Alpaca", "kraken_paper": "Kraken (conto simulato)"}.get(bname, bname)

    def broker_account():
        from .broker import get_broker
        b = get_broker(cfg)
        a = b.account()
        if bname == "alpaca":
            return f"{'DEMO' if ENV.alpaca_paper else '⚠️ REALE'} · equity {a['equity']:,.2f}$ · mercato USA {'aperto' if b.market_open() else 'chiuso'}"
        return f"SIMULATO sui prezzi veri · equity {a['equity']:,.2f}$ · nessun ordine reale"
    out.append(_try(f"{label} conto", broker_account))

    def broker_data():
        from .broker import get_broker
        b = get_broker(cfg)
        res = []
        for sym in cfg["universe"]["crypto"][:1] + cfg["universe"]["stocks"][:1]:
            df = b.bars(sym, "1Hour")
            res.append(f"{sym} {len(df)} barre 1H, ultimo {df['close'].iloc[-1]:,.2f}")
        if hasattr(b, "funding_rates"):
            fr = b.funding_rates()
            res.append("funding orario " + ", ".join(f"{k} {v:+.5%}" for k, v in fr.items()) if fr else "funding n/d")
        return " · ".join(res)
    out.append(_try(f"{label} dati", broker_data))

    def claude():
        if not ENV.anthropic_key:
            raise RuntimeError("ANTHROPIC_API_KEY vuota: il bot userà le regole di riserva")
        import anthropic
        m = anthropic.Anthropic().models.retrieve(ENV.claude_model)  # nessun costo in token
        return f"modello {m.id} raggiungibile"
    out.append(_try("Claude API", claude))

    def tg():
        if not (ENV.tg_token and ENV.tg_chat):
            raise RuntimeError("TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID mancanti")
        from . import telegram
        me = telegram._call("getMe")["result"]["username"]
        if telegram_test:
            telegram.send("✅ Test riuscito: il bot TradeBot riesce a scriverti.")
        return f"@{me}" + (" · messaggio di prova inviato" if telegram_test else "")
    out.append(_try("Telegram", tg))

    def feeds():
        from . import macro
        fg, ev, fr = macro.fear_greed(), macro.economic_events(), macro.funding_rate("BTC/USD")
        if fg is None and not ev and fr is None:
            raise RuntimeError("nessuna fonte macro raggiungibile (il bot funziona comunque)")
        return f"Fear&Greed {fg['today'] if fg else 'n/d'} · eventi USA settimana {len(ev)} · funding BTC {fr if fr is not None else 'n/d'}"
    out.append(_try("Fonti macro/sentiment", feeds))
    return out


if __name__ == "__main__":
    results = run_checks("--telegram-test" in sys.argv)
    for name, ok, detail in results:
        print(f"{'OK ' if ok else 'ERR'}  {name:24s} {detail}")
    critical = results[:2]   # conto e dati del broker
    sys.exit(0 if all(ok for _, ok, _ in critical) else 1)
