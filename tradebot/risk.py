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
    per_unit = price - stop
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
    if qty * price < 10:  # sotto i 10$ non ha senso
        return 0.0, "esposizione massima raggiunta"
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
            db.kv_set("cooldown_until", (now + timedelta(hours=24)).timestamp())
            out["reasons"].append(f"{len(last)} perdite consecutive → pausa 24h (anti revenge-trading)")
    if db.cooldown_active():
        out["risk_mult"] = 0.0
        out["state"] = out["state"] if out["state"] in ("kill", "halt_today") else "cooldown"
    return out
