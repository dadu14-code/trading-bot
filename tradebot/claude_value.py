"""Claude vale quello che costa? Confronto fra il bot reale (guidato da Claude) e il
portafoglio ombra "senza Claude" (stesse regole, direttiva automatica gratuita).
Stesso metodo per entrambi: solo operazioni aperte dall'inizio del confronto,
P&L realizzato + non realizzato ai prezzi attuali."""
from datetime import datetime, timezone

from . import db


def _pnl(rows: list[dict], price_fn) -> tuple[float, int, int]:
    total, n_open, prices = 0.0, 0, {}
    for r in rows:
        if r["status"] == "closed":
            total += r["pnl"] or 0
        elif r["status"] == "open" and price_fn:
            if r["symbol"] not in prices:
                try:
                    prices[r["symbol"]] = price_fn(r["symbol"])
                except Exception:
                    prices[r["symbol"]] = r["entry_price"]
            sgn = -1 if r.get("side") == "short" else 1
            total += sgn * (prices[r["symbol"]] - r["entry_price"]) * r["qty"]
            n_open += 1
    return total, len([r for r in rows if r["status"] in ("open", "closed")]), n_open


def compare(price_fn=None) -> dict | None:
    start = db.kv_get("noclaude_start")
    if not start:
        return None
    with db.conn() as c:
        real = [dict(r) for r in c.execute("SELECT * FROM trades WHERE entry_ts>=? AND status!='void'", (start,))]
        shadow = [dict(r) for r in c.execute("SELECT * FROM shadow_trades WHERE variant='senza_claude' AND entry_ts>=?", (start,))]
        eq = c.execute("SELECT equity FROM equity WHERE ts>=? ORDER BY ts LIMIT 1", (start,)).fetchone()
    with_c, n_with, open_with = _pnl(real, price_fn)
    without, n_without, open_without = _pnl(shadow, price_fn)
    cost = db.llm_usage_since(start)["cost_usd"]
    days = (datetime.now(timezone.utc) - datetime.fromisoformat(start)).total_seconds() / 86400
    return {"since": start, "days": days, "equity_ref": eq["equity"] if eq else None,
            "with_claude": with_c, "n_with": n_with, "open_with": open_with,
            "without_claude": without, "n_without": n_without, "open_without": open_without,
            "edge": with_c - without, "cost": cost, "value": with_c - without - cost}
