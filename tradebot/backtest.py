"""Backtest walk-forward del motore quantitativo (senza LLM).

Uso:
  python -m tradebot.backtest --symbol BTC/USD --days 730     # dati Alpaca (servono le chiavi)
  python -m tradebot.backtest --csv dati_1h.csv               # CSV con timestamp,open,high,low,close,volume
  python -m tradebot.backtest --synthetic                     # dati finti, solo per verificare che giri
"""
import argparse
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from . import indicators as ind
from . import signals
from .config import load_settings
from .stats import max_drawdown, trade_stats

AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def synthetic(n=6000, seed=7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    regime = np.repeat(rng.choice([-1, 0, 1], size=n // 500 + 1), 500)[:n]
    r = rng.normal(0.00025 * regime, 0.008)
    c = 100 * np.exp(np.cumsum(r))
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + abs(rng.normal(0, 0.003, n)))
    l = np.minimum(o, c) * (1 - abs(rng.normal(0, 0.003, n)))
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": rng.lognormal(10, 0.5, n)}, index=idx)


def run(df1h: pd.DataFrame, cfg: dict, equity0=10_000.0, fee=0.0025, step=4, warmup=1500) -> dict:
    rk = cfg["risk"]
    df4 = df1h.resample("4h").agg(AGG).dropna()
    df1d = df1h.resample("1D").agg(AGG).dropna()
    atr4_all = ind.atr(df4)
    equity, pos, trades, curve = equity0, None, [], []
    for i in range(warmup, len(df1h)):
        t, bar = df1h.index[i], df1h.iloc[i]
        a4 = float(atr4_all[:t].iloc[-1])
        if pos:
            pos["highest"] = max(pos["highest"], bar["high"])
            hit = bar["low"] <= pos["stop"]
            if hit or i % step == 0:
                lt_label = "BULL"
                if i % 24 == 0:
                    from .regime import long_term
                    lt_label = long_term(df1d[:t].iloc[:-1] if len(df1d[:t]) > 1 else df1d[:t])["label"]
                bars4 = int((t - pos["entry_ts"]) / pd.Timedelta(hours=4))
                reason, new_stop = signals.evaluate_exit(pos, bar["close"], a4, bars4, lt_label, None, cfg)
                if hit:
                    reason, px = "stop", min(pos["stop"], bar["open"])
                else:
                    px = bar["close"]
                pos["stop"] = new_stop
                if reason:
                    pnl = (px * (1 - fee) - pos["entry_price"] * (1 + fee)) * pos["qty"]
                    equity += pnl
                    trades.append({"pnl": pnl, "r_multiple": (px - pos["entry_price"]) / (pos["entry_price"] - pos["initial_stop"]),
                                   "entry_ts": pos["entry_ts"], "exit_ts": t, "reason": reason})
                    pos = None
        elif i % step == 0:
            bars = {"short": df1h.iloc[: i + 1], "medium": df4[:t], "long": df1d[:t]}
            ev = signals.evaluate_entry("SIM", bars, None, cfg)
            if ev["go"]:
                px = bar["close"]
                stop = px - rk["atr_stop_mult"] * a4
                qty = min(equity * rk["risk_per_trade_pct"] / 100 / (px - stop),
                          equity * rk["max_position_pct"] / 100 / px)
                pos = {"entry_price": px, "initial_stop": stop, "stop": stop, "highest": px, "qty": qty, "entry_ts": t}
        mtm = equity + ((bar["close"] - pos["entry_price"]) * pos["qty"] if pos else 0)
        curve.append(mtm)
    bh = df1h["close"].iloc[-1] / df1h["close"].iloc[warmup] - 1
    return {"stats": trade_stats(trades), "final_equity": curve[-1] if curve else equity0,
            "return": (curve[-1] / equity0 - 1) if curve else 0, "max_dd": max_drawdown(curve),
            "buy_and_hold": float(bh), "trades": trades}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol")
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--csv")
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args()
    cfg = load_settings()
    if a.synthetic:
        df = synthetic()
    elif a.csv:
        df = pd.read_csv(a.csv, parse_dates=[0], index_col=0)
        df.index = pd.to_datetime(df.index, utc=True)
    else:
        from .broker import AlpacaBroker, TF, LOOKBACK
        LOOKBACK["1Hour"] = (a.days, a.days)
        df = AlpacaBroker().bars(a.symbol, "1Hour")
    res = run(df, cfg)
    s = res["stats"]
    print(f"Barre: {len(df)}  Operazioni: {s.get('n', 0)}")
    if s.get("n"):
        print(f"Win rate {s['win_rate']:.1%} · PF {s['profit_factor']:.2f} · media {s['avg_r']:+.2f}R")
    print(f"Rendimento {res['return']:+.2%} · Max DD {res['max_dd']:.2%} · Buy&Hold {res['buy_and_hold']:+.2%}")


if __name__ == "__main__":
    main()
