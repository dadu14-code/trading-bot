"""Lettura del regime di mercato su più orizzonti temporali.

Ogni orizzonte produce un punteggio -1 (ribassista) .. +1 (rialzista):
  long   (1D)  – il "fiume": sopra/sotto SMA200, pendenza SMA50, forza ADX
  medium (4H)  – la "corrente": EMA20 vs EMA50, momentum, struttura dei massimi
  short  (1H)  – l'"onda": serve solo per il timing d'ingresso
"""
import numpy as np
import pandas as pd

from . import indicators as ind


def long_term(df: pd.DataFrame) -> dict:
    c = df["close"]
    s200 = ind.sma(c, 200) if len(c) >= 200 else ind.sma(c, max(len(c) // 2, 20))
    s50 = ind.sma(c, 50)
    slope50 = (s50.iloc[-1] / s50.iloc[-11] - 1) if len(s50.dropna()) > 11 else 0
    adx = float(ind.adx(df).iloc[-1])
    above200 = c.iloc[-1] > s200.iloc[-1]
    dd_from_high = c.iloc[-1] / c.rolling(252, min_periods=20).max().iloc[-1] - 1
    vol = c.pct_change().rolling(20).std().iloc[-1] * np.sqrt(365)
    score = (0.45 if above200 else -0.45) + float(np.clip(slope50 * 10, -0.35, 0.35))
    score += 0.2 * np.sign(score) * min(adx / 40, 1)
    label = "BULL" if score > 0.35 else "BEAR" if score < -0.35 else "RANGE"
    return {"score": round(float(np.clip(score, -1, 1)), 3), "label": label,
            "above_sma200": bool(above200), "sma50_slope_10d": round(float(slope50), 4),
            "adx": round(adx, 1), "drawdown_from_1y_high": round(float(dd_from_high), 4),
            "annual_vol": round(float(vol), 3)}


def medium_term(df: pd.DataFrame) -> dict:
    c = df["close"]
    e20, e50 = ind.ema(c, 20), ind.ema(c, 50)
    mom = c.iloc[-1] / c.iloc[-31] - 1 if len(c) > 31 else 0
    hh = df["high"].iloc[-10:].max() > df["high"].iloc[-20:-10].max()
    hl = df["low"].iloc[-10:].min() > df["low"].iloc[-20:-10].min()
    score = (0.4 if e20.iloc[-1] > e50.iloc[-1] else -0.4)
    score += float(np.clip(mom * 5, -0.35, 0.35))
    score += 0.25 if (hh and hl) else -0.25 if (not hh and not hl) else 0
    return {"score": round(float(np.clip(score, -1, 1)), 3), "ema20_gt_ema50": bool(e20.iloc[-1] > e50.iloc[-1]),
            "momentum_30bars": round(float(mom), 4), "higher_highs_lows": bool(hh and hl),
            "atr": float(ind.atr(df).iloc[-1])}


def short_term_trigger(df: pd.DataFrame) -> dict:
    """Trigger d'ingresso: pullback in trend o breakout con volume."""
    c = df["close"]
    e20 = ind.ema(c, 20)
    r = ind.rsi(c)
    dch = ind.donchian_high(df, 20)
    vol_ok = df["volume"].iloc[-1] > 1.3 * df["volume"].rolling(20).mean().iloc[-1]
    pullback = bool(r.iloc[-6:-1].min() < 42 and r.iloc[-1] > r.iloc[-2] and c.iloc[-1] > e20.iloc[-1])
    breakout = bool(c.iloc[-1] > dch.iloc[-1] and vol_ok)
    overext = bool(r.iloc[-1] > 78)
    score = 0.0
    if pullback:
        score += 0.7
    if breakout:
        score += 0.6
    if overext:
        score -= 0.6   # mai inseguire un prezzo esteso
    return {"score": round(float(np.clip(score, -1, 1)), 3), "pullback": pullback, "breakout": breakout,
            "overextended": overext, "rsi": round(float(r.iloc[-1]), 1)}


def price_levels(daily: pd.DataFrame, h4: pd.DataFrame) -> dict:
    """Livelli di prezzo concreti per il trader senior: medie, massimi/minimi, pivot."""
    c = daily["close"]
    r = lambda x: round(float(x), 2)
    piv = []
    hi, lo = daily["high"].to_numpy(), daily["low"].to_numpy()
    for i in range(len(daily) - 3, max(len(daily) - 90, 2), -1):   # swing degli ultimi ~3 mesi
        if hi[i] == hi[i - 2:i + 3].max():
            piv.append(("resistenza", r(hi[i]), str(daily.index[i].date())))
        if lo[i] == lo[i - 2:i + 3].min():
            piv.append(("supporto", r(lo[i]), str(daily.index[i].date())))
    return {
        "last": r(c.iloc[-1]),
        "sma50_1d": r(ind.sma(c, 50).iloc[-1]), "sma200_1d": r(ind.sma(c, 200).iloc[-1]) if len(c) >= 200 else None,
        "ema20_4h": r(ind.ema(h4["close"], 20).iloc[-1]), "ema50_4h": r(ind.ema(h4["close"], 50).iloc[-1]),
        "high_20d": r(daily["high"].iloc[-20:].max()), "low_20d": r(daily["low"].iloc[-20:].min()),
        "high_55d": r(daily["high"].iloc[-55:].max()), "low_55d": r(daily["low"].iloc[-55:].min()),
        "prev_week_high": r(daily["high"].iloc[-7:].max()), "prev_week_low": r(daily["low"].iloc[-7:].min()),
        "atr_1d": r(ind.atr(daily).iloc[-1]),
        "recent_swing_pivots": [{"type": t, "price": p, "date": d} for t, p, d in piv[:8]],
    }


def short_term_trigger_short(df: pd.DataFrame) -> dict:
    """Specchio del trigger long: rimbalzo che fallisce sotto la media o rottura dei minimi."""
    c = df["close"]
    e20 = ind.ema(c, 20)
    r = ind.rsi(c)
    dcl = df["low"].rolling(20).min().shift(1)
    vol_ok = df["volume"].iloc[-1] > 1.3 * df["volume"].rolling(20).mean().iloc[-1]
    failed_rally = bool(r.iloc[-6:-1].max() > 58 and r.iloc[-1] < r.iloc[-2] and c.iloc[-1] < e20.iloc[-1])
    breakdown = bool(c.iloc[-1] < dcl.iloc[-1] and vol_ok)
    oversold = bool(r.iloc[-1] < 22)
    score = (0.7 if failed_rally else 0) + (0.6 if breakdown else 0) - (0.6 if oversold else 0)
    return {"score": round(float(np.clip(score, -1, 1)), 3), "failed_rally": failed_rally, "breakdown": breakdown,
            "oversold": oversold, "rsi": round(float(r.iloc[-1]), 1)}


def capitulation(daily: pd.DataFrame, h4: pd.DataFrame, h1: pd.DataFrame) -> dict:
    """Riconosce una capitolazione: crollo esteso + volume climax + primo segnale di inversione."""
    a4 = ind.atr(h4)
    e20 = ind.ema(h4["close"], 20)
    look = 6                                                  # ultime 6 barre 4H ≈ 24 ore
    stretch = float(((e20 - h4["low"]) / a4).iloc[-look:].max())        # quanti ATR sotto la media
    rsi_d = ind.rsi(daily["close"])
    drop_10d = float(daily["close"].iloc[-1] / daily["high"].iloc[-10:].max() - 1)
    vol_climax = float((h4["volume"].iloc[-look:] / h4["volume"].rolling(50).mean().iloc[-look:]).max())
    cap_low = float(h4["low"].iloc[-look:].min())
    # inversione: candela di rigetto sull'ultima 4H chiusa, o 1H che recupera il massimo precedente
    last4 = h4.iloc[-2:]
    body = abs(last4["close"].iloc[-1] - last4["open"].iloc[-1])
    lower_wick = min(last4["open"].iloc[-1], last4["close"].iloc[-1]) - last4["low"].iloc[-1]
    hammer4 = bool(lower_wick > 2 * body and last4["close"].iloc[-1] > last4["low"].iloc[-1] + 0.6 * (last4["high"].iloc[-1] - last4["low"].iloc[-1]))
    engulf4 = bool(last4["close"].iloc[-2] < last4["open"].iloc[-2] and last4["close"].iloc[-1] > last4["open"].iloc[-2])
    reclaim1 = bool(h1["close"].iloc[-1] > h1["high"].iloc[-4:-1].max() and h1["close"].iloc[-1] > cap_low + float(a4.iloc[-1]) * 0.5)
    crash = stretch >= 2.5 or float(rsi_d.iloc[-3:].min()) < 25 or drop_10d <= -3 * float(ind.atr(daily).iloc[-1]) / float(daily["close"].iloc[-1])
    climax = vol_climax >= 2.0
    reversal = hammer4 or engulf4 or reclaim1
    return {"crash": bool(crash), "climax_volume": bool(climax), "reversal": bool(reversal),
            "stretch_atr": round(stretch, 2), "rsi_daily_min3": round(float(rsi_d.iloc[-3:].min()), 1),
            "drop_from_10d_high": round(drop_10d, 4), "volume_vs_avg": round(vol_climax, 2),
            "cap_low": cap_low, "atr_4h": float(a4.iloc[-1]),
            "signals": [n for n, v in (("martello 4H", hammer4), ("engulfing 4H", engulf4), ("recupero 1H", reclaim1)) if v]}
