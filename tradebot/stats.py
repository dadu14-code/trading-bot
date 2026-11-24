"""Statistiche di performance + checklist "pronto per il conto reale"."""
from datetime import datetime, timezone

import numpy as np


def trade_stats(trades: list[dict]) -> dict:
    pnl = np.array([t["pnl"] or 0 for t in trades], dtype=float)
    r = np.array([t["r_multiple"] or 0 for t in trades], dtype=float)
    if len(pnl) == 0:
        return {"n": 0}
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    return {"n": len(pnl), "win_rate": float((pnl > 0).mean()), "pnl": float(pnl.sum()),
            "avg_r": float(r.mean()), "expectancy": float(pnl.mean()),
            "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) else float("inf"),
            "best": float(pnl.max()), "worst": float(pnl.min())}


def max_drawdown(equity: list[float]) -> float:
    if not equity:
        return 0.0
    e = np.array(equity, dtype=float)
    return float((e / np.maximum.accumulate(e) - 1).min())


def go_live_readiness(trades: list[dict], equity_rows: list[dict]) -> list[tuple[str, bool]]:
    s = trade_stats(trades)
    days = 0
    if equity_rows:
        first = datetime.fromisoformat(equity_rows[0]["ts"])
        days = (datetime.now(timezone.utc) - first).days
    dd = max_drawdown([e["equity"] for e in equity_rows])
    return [
        (f"Almeno 8 settimane di demo ({days} gg)", days >= 56),
        (f"Almeno 40 operazioni chiuse ({s.get('n', 0)})", s.get("n", 0) >= 40),
        (f"Profit factor ≥ 1.3 ({s.get('profit_factor', 0):.2f})", s.get("profit_factor", 0) >= 1.3),
        (f"Expectancy > 0 ({s.get('avg_r', 0):+.2f}R per trade)", s.get("avg_r", 0) > 0),
        (f"Max drawdown < 10% ({dd:.1%})", dd > -0.10),
    ]
