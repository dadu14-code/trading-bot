"""Pattern e "memoria storica" del mercato.

1) Pattern candlestick classici (conferme di breve).
2) Motore di ANALOGIE STORICHE: descrive la situazione attuale con un vettore
   di caratteristiche (momentum su più orizzonti, RSI, volatilità, distanza
   dalle medie) e cerca nel passato dello stesso asset le K situazioni più
   simili. Guarda poi cosa è successo DOPO quelle situazioni: rendimento
   atteso, % di casi positivi, peggior escursione. È il modo quantitativo con
   cui un trader esperto dice "questa l'ho già vista".
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import indicators as ind


def candle_patterns(df: pd.DataFrame) -> dict:
    o, h, l, c = (df[k].iloc[-3:].to_numpy() for k in ("open", "high", "low", "close"))
    body = abs(c[-1] - o[-1])
    rng = max(h[-1] - l[-1], 1e-12)
    lower_wick = min(o[-1], c[-1]) - l[-1]
    upper_wick = h[-1] - max(o[-1], c[-1])
    return {
        "bullish_engulfing": bool(c[-2] < o[-2] and c[-1] > o[-1] and c[-1] >= o[-2] and o[-1] <= c[-2]),
        "bearish_engulfing": bool(c[-2] > o[-2] and c[-1] < o[-1] and o[-1] >= c[-2] and c[-1] <= o[-2]),
        "hammer": bool(lower_wick > 2 * body and upper_wick < body and body / rng < 0.35),
        "shooting_star": bool(upper_wick > 2 * body and lower_wick < body and body / rng < 0.35),
        "inside_bar": bool(h[-1] < h[-2] and l[-1] > l[-2]),
    }


def features(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    a = ind.atr(df)
    f = pd.DataFrame(index=df.index)
    f["r1"] = c.pct_change(1)
    f["r6"] = c.pct_change(6)
    f["r24"] = c.pct_change(24)
    f["r72"] = c.pct_change(72)
    f["rsi"] = ind.rsi(c) / 100
    f["atrp"] = a / c
    f["d_ema20"] = (c - ind.ema(c, 20)) / a
    f["d_ema100"] = (c - ind.ema(c, 100)) / a
    f["vol_ratio"] = np.log((df["volume"] + 1) / (df["volume"].rolling(24).mean() + 1))
    return f


@dataclass
class AnalogResult:
    n: int
    exp_return: float     # rendimento medio a orizzonte H
    win_rate: float       # % casi con rendimento > 0
    median_mae: float     # escursione avversa mediana (rischio tipico)
    score: float          # -1..+1, sintesi per il motore segnali

    def as_dict(self):
        return {k: round(v, 4) if isinstance(v, float) else v for k, v in self.__dict__.items()}


def analogs(df: pd.DataFrame, k: int = 40, horizon: int = 24, min_history: int = 300) -> AnalogResult | None:
    if len(df) < min_history:
        return None
    f = features(df).replace([np.inf, -np.inf], np.nan)
    c = df["close"].to_numpy()
    lo = df["low"].to_numpy()
    # z-score sulle sole righe storiche, niente look-ahead
    hist = f.iloc[: -horizon - 1].dropna()
    if len(hist) < k * 3:
        return None
    mu, sd = hist.mean(), hist.std().replace(0, 1)
    X = ((hist - mu) / sd).to_numpy()
    now = ((f.iloc[-1] - mu) / sd).to_numpy()
    if np.isnan(now).any():
        return None
    dist = np.sqrt(((X - now) ** 2).sum(axis=1))
    # evita analogie sovrapposte: prendi i più vicini distanziati di almeno `horizon` barre
    pos = np.array([df.index.get_loc(t) for t in hist.index])
    chosen = []
    for i in np.argsort(dist):
        if all(abs(pos[i] - p) >= horizon // 2 for p in chosen):
            chosen.append(pos[i])
        if len(chosen) >= k:
            break
    rets, maes = [], []
    for p in chosen:
        rets.append(c[p + horizon] / c[p] - 1)
        maes.append(lo[p + 1: p + horizon + 1].min() / c[p] - 1)
    rets, maes = np.array(rets), np.array(maes)
    exp_r, wr = float(rets.mean()), float((rets > 0).mean())
    # punteggio: expectancy normalizzata per la dispersione + scarto della win-rate da 50%
    sharpe_like = exp_r / (rets.std() + 1e-9)
    score = float(np.clip(0.6 * np.tanh(3 * sharpe_like) + 0.4 * (wr - 0.5) * 4, -1, 1))
    return AnalogResult(len(rets), exp_r, wr, float(np.median(maes)), score)
