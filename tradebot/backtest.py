"""Backtest walk-forward del motore quantitativo (senza LLM), in due fasi:

  1) precompute(): calcola UNA volta regimi, trigger e analogie storiche barra per
     barra (la parte lenta, senza look-ahead: ogni barra vede solo il passato);
  2) simulate(): applica soglie, veti, stop e trailing → velocissimo, così
     l'ottimizzatore può provare centinaia di combinazioni.

Uso:
  python -m tradebot.backtest --symbol BTC/USD --days 730     # dati Alpaca (servono le chiavi)
  python -m tradebot.backtest --csv dati_1h.csv               # CSV: timestamp,open,high,low,close,volume
  python -m tradebot.backtest --synthetic                     # dati finti, solo per verificare che giri
"""
import argparse

import numpy as np
import pandas as pd

from . import indicators as ind
from . import patterns, regime
from .config import load_settings
from .stats import max_drawdown, trade_stats

AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
FEES = {"crypto": 0.0025, "stock": 0.0005}   # commissione+slippage per lato


def synthetic(n=6000, seed=7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    reg = np.repeat(rng.choice([-1, 0, 1], size=n // 500 + 1), 500)[:n]
    r = rng.normal(0.00025 * reg, 0.008)
    c = 100 * np.exp(np.cumsum(r))
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + abs(rng.normal(0, 0.003, n)))
    l = np.minimum(o, c) * (1 - abs(rng.normal(0, 0.003, n)))
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": rng.lognormal(10, 0.5, n)}, index=idx)


def precompute(df1h: pd.DataFrame, cfg: dict, step: int = 4, warmup: int = 1500) -> dict:
    sc = cfg["signals"]
    w = sc["weights"]
    df4 = df1h.resample("4h").agg(AGG).dropna()
    df1d = df1h.resample("1D").agg(AGG).dropna()
    norm = lambda x: (max(-1, min(1, x)) + 1) / 2
    # barre 4H/1D COMPLETATE prima dell'istante t (niente candela in formazione dal futuro)
    sig = {}
    for i in range(warmup, len(df1h), step):
        t = df1h.index[i]
        s4 = df4[df4.index + pd.Timedelta(hours=4) <= t + pd.Timedelta(hours=1)]
        s1d = df1d[df1d.index + pd.Timedelta(days=1) <= t + pd.Timedelta(hours=1)]
        if len(s4) < 60 or len(s1d) < 60:
            continue
        sh = df1h.iloc[: i + 1]
        lt, mt, st = regime.long_term(s1d), regime.medium_term(s4), regime.short_term_trigger(sh)
        an = patterns.analogs(sh, k=sc["analog_k"], horizon=sc["analog_horizon_bars"])
        cp = patterns.candle_patterns(sh)
        trig = st["score"] + (0.2 if cp["bullish_engulfing"] or cp["hammer"] else 0) - (0.2 if cp["bearish_engulfing"] else 0)
        comp = (w["long"] * norm(lt["score"]) + w["medium"] * norm(mt["score"])
                + w["trigger"] * norm(trig) + w["analog"] * norm(an.score if an else 0))
        sig[i] = {"composite": comp, "long_label": lt["label"], "trigger": st["score"],
                  "overext": st["overextended"], "an_wr": an.win_rate if an else None, "atr4": mt["atr"]}
    daily_label = {}
    for d in range(60, len(df1d)):
        daily_label[df1d.index[d]] = regime.long_term(df1d.iloc[: d])["label"]  # giorno d escluso
    return {"df": df1h, "sig": sig, "daily_label": pd.Series(daily_label), "atr4": ind.atr(df4), "warmup": warmup}


def simulate(pre: dict, params: dict, fee: float = 0.0025, equity0: float = 10_000.0,
             start: int | None = None, end: int | None = None) -> dict:
    """params: entry_threshold, analog_min_winrate, atr_stop_mult, atr_trail_mult, time_stop_bars_4h, risk_per_trade_pct, max_position_pct"""
    df, sig = pre["df"], pre["sig"]
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    idx = df.index
    lab = pre["daily_label"]
    atr4 = pre["atr4"]
    start = max(start or pre["warmup"], pre["warmup"])
    end = min(end or len(df), len(df))
    equity, pos, trades, curve = equity0, None, [], []
    last_atr = None
    for i in range(start, end):
        if i in sig:
            last_atr = sig[i]["atr4"]
        if pos:
            pos["highest"] = max(pos["highest"], h[i])
            exit_px, reason = None, None
            if l[i] <= pos["stop"]:
                exit_px, reason = min(pos["stop"], o[i]), "stop"
            elif i in sig:
                a = sig[i]["atr4"]
                pos["stop"] = max(pos["stop"], pos["highest"] - params["atr_trail_mult"] * a)
                one_r = pos["entry"] - pos["init_stop"]
                if c[i] - pos["entry"] >= one_r:
                    pos["stop"] = max(pos["stop"], pos["entry"])
                d = lab.index.searchsorted(idx[i], side="right") - 1
                bars4 = (i - pos["i"]) // 4
                if d >= 0 and lab.iloc[d] == "BEAR":
                    exit_px, reason = c[i], "trend"
                elif bars4 >= params["time_stop_bars_4h"] and c[i] < pos["entry"] + 0.5 * one_r:
                    exit_px, reason = c[i], "time"
            if exit_px is not None:
                pnl = (exit_px * (1 - fee) - pos["entry"] * (1 + fee)) * pos["qty"]
                equity += pnl
                trades.append({"pnl": pnl, "r_multiple": (exit_px - pos["entry"]) / (pos["entry"] - pos["init_stop"]),
                               "entry_ts": idx[pos["i"]], "exit_ts": idx[i], "reason": reason})
                pos = None
        elif i in sig:
            s = sig[i]
            go = (s["composite"] >= params["entry_threshold"] and s["long_label"] != "BEAR" and not s["overext"]
                  and s["trigger"] > 0 and (s["an_wr"] is None or s["an_wr"] >= params["analog_min_winrate"]))
            if go and i + 1 < len(df):
                entry = o[i + 1]                           # eseguito all'apertura della barra successiva
                stop = entry - params["atr_stop_mult"] * s["atr4"]
                qty = min(equity * params["risk_per_trade_pct"] / 100 / (entry - stop),
                          equity * params["max_position_pct"] / 100 / entry)
                pos = {"entry": entry * 1.0, "init_stop": stop, "stop": stop, "highest": entry, "qty": qty, "i": i + 1}
        curve.append(equity + ((c[i] - pos["entry"]) * pos["qty"] if pos else 0))
    st = trade_stats(trades)
    bh = c[end - 1] / c[start] - 1 if end > start else 0
    return {"stats": st, "return": curve[-1] / equity0 - 1 if curve else 0, "max_dd": max_drawdown(curve),
            "buy_and_hold": float(bh), "trades": trades, "curve": curve}


def params_from(cfg: dict) -> dict:
    s, r = cfg["signals"], cfg["risk"]
    return {"entry_threshold": s["entry_threshold"], "analog_min_winrate": s["analog_min_winrate"],
            "atr_stop_mult": r["atr_stop_mult"], "atr_trail_mult": r["atr_trail_mult"],
            "time_stop_bars_4h": r["time_stop_bars_4h"], "risk_per_trade_pct": r["risk_per_trade_pct"],
            "max_position_pct": r["max_position_pct"]}


def run(df1h: pd.DataFrame, cfg: dict, fee=0.0025, warmup=1500) -> dict:
    return simulate(precompute(df1h, cfg, warmup=warmup), params_from(cfg), fee=fee)


def load(symbol=None, days=730, csv=None, synthetic_data=False) -> pd.DataFrame:
    if synthetic_data:
        return synthetic()
    if csv:
        df = pd.read_csv(csv, parse_dates=[0], index_col=0)
        df.index = pd.to_datetime(df.index, utc=True)
        return df
    from .broker import AlpacaBroker, LOOKBACK
    LOOKBACK["1Hour"] = (days, days)
    return AlpacaBroker().bars(symbol, "1Hour")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol")
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--csv")
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args()
    cfg = load_settings()
    df = load(a.symbol, a.days, a.csv, a.synthetic)
    fee = FEES["crypto" if (a.symbol or "/").find("/") >= 0 else "stock"]
    res = run(df, cfg, fee=fee)
    s = res["stats"]
    print(f"Barre: {len(df)}  Operazioni: {s.get('n', 0)}")
    if s.get("n"):
        print(f"Win rate {s['win_rate']:.1%} · PF {s['profit_factor']:.2f} · media {s['avg_r']:+.2f}R")
    print(f"Rendimento {res['return']:+.2%} · Max DD {res['max_dd']:.2%} · Buy&Hold {res['buy_and_hold']:+.2%}")


if __name__ == "__main__":
    main()
