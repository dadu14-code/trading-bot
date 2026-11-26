"""Gestione del rischio: dimensionamento posizioni e circuit breaker.

Il rischio viene PRIMA del rendimento: queste regole non possono essere
scavalcate né dai segnali né dal modello linguistico.
"""
from datetime import datetime, timedelta, timezone

from . import db


def position_size(equity: float, price: float, stop: float, risk_mult: float, cfg: dict,
                  current_exposure: float, crypto_exposure: float, is_crypto: bool) -> tuple[float, str]:
    rk = cfg["risk"]
    risk_mult = max(0.0, min(1.5, risk_mult))            # clamp della fiducia dell'LLM
    per_unit = abs(price - stop)
    if per_unit <= 0 or risk_mult == 0:
        return 0.0, "rischio unitario nullo"
    risk_cash = equity * rk["risk_per_trade_pct"] / 100 * risk_mult
    qty = risk_cash / per_unit
    caps = [equity * rk["max_position_pct"] / 100,
            equity * rk["max_total_exposure_pct"] / 100 - current_exposure]
    if is_crypto:
        caps.append(equity * rk["max_crypto_exposure_pct"] / 100 - crypto_exposure)
    max_notional = max(0.0, min(caps))
    qty = min(qty, max_notional / price)
    if qty * price < rk.get("min_order_usd", 10):
        return 0.0, "sotto l'ordine minimo o esposizione massima raggiunta"
    return (round(qty, 6) if is_crypto else int(qty)), "ok"


def circuit_breaker(equity: float, cfg: dict) -> dict:
    """Valuta lo stato del conto: ok | derisk | halt_today | kill."""
    rk = cfg["risk"]
    now = datetime.now(timezone.utc)
    out = {"state": "ok", "risk_mult": 1.0, "reasons": []}

    day = db.equity_since((now - timedelta(hours=24)).isoformat())
    week = db.equity_since((now - timedelta(days=7)).isoformat())
    peak = db.peak_equity() or equity
    if day and (equity / day[0]["equity"] - 1) * 100 <= -rk["daily_loss_halt_pct"]:
        out.update(state="halt_today", risk_mult=0.0)
        out["reasons"].append(f"perdita 24h oltre {rk['daily_loss_halt_pct']}%")
    if week and (equity / week[0]["equity"] - 1) * 100 <= -rk["weekly_loss_derisk_pct"]:
        out["risk_mult"] = min(out["risk_mult"], 0.5)
        out["state"] = out["state"] if out["state"] != "ok" else "derisk"
        out["reasons"].append(f"perdita settimanale oltre {rk['weekly_loss_derisk_pct']}% → rischio dimezzato")
    dd = (equity / peak - 1) * 100
    if dd <= -rk["max_drawdown_kill_pct"]:
        out.update(state="kill", risk_mult=0.0)
        out["reasons"].append(f"drawdown {dd:.1f}% dal massimo: KILL SWITCH")

    last = db.closed_trades(limit=rk["losing_streak_cooldown"])
    if len(last) == rk["losing_streak_cooldown"] and all((t["pnl"] or 0) < 0 for t in last):
        if not db.kv_get(f"cooldown_for_{last[0]['id']}"):
            db.kv_set(f"cooldown_for_{last[0]['id']}", True)
            hrs = rk.get("losing_streak_cooldown_hours", 8)
            db.kv_set("cooldown_until", (now + timedelta(hours=hrs)).timestamp())
            out["reasons"].append(f"{len(last)} perdite consecutive → pausa {hrs}h (anti revenge-trading)")
    if db.cooldown_active():
        out["risk_mult"] = 0.0
        out["state"] = out["state"] if out["state"] in ("kill", "halt_today") else "cooldown"
    return out


def vol_scaler(cfg: dict) -> tuple[float, float | None]:
    """Volatility targeting sul portafoglio: se la curva dell'equity degli ultimi 7
    giorni oscilla più del target annuo, riduce il rischio in proporzione."""
    import numpy as np
    target = cfg["risk"].get("target_portfolio_vol")
    rows = db.equity_since((datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
    if not target or len(rows) < 50:
        return 1.0, None
    eq = np.array([r["equity"] for r in rows])
    ts = [datetime.fromisoformat(r["ts"]) for r in rows]
    step_min = max((ts[-1] - ts[0]).total_seconds() / 60 / (len(ts) - 1), 1)
    rets = np.diff(eq) / eq[:-1]
    vol = float(rets.std() * np.sqrt(365 * 24 * 60 / step_min))
    return (float(min(1.0, target / vol)) if vol > 0 else 1.0), vol
