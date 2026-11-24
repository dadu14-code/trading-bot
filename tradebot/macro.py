"""Contesto macro e sentiment (fonti pubbliche gratuite, senza chiavi).

  • Calendario economico: eventi ad alto impatto (FOMC, CPI, NFP…). Un trader
    esperto non apre posizioni nuove mezz'ora prima della Fed.
  • Crypto Fear & Greed Index (alternative.me).
  • Funding rate dei perpetual (Binance, riserva Bybit): leva eccessiva = rischio
    di liquidazioni a cascata.
Ogni fonte può fallire: in quel caso si torna None e il bot prosegue senza.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)
_cache: dict = {}
UA = {"User-Agent": "tradebot/1.0"}


def _get_json(url: str, ttl: int, **params):
    key = (url, tuple(sorted(params.items())))
    if key in _cache and time.time() - _cache[key][0] < ttl:
        return _cache[key][1]
    try:
        r = requests.get(url, params=params, headers=UA, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # rete, formato, rate limit
        log.info("fonte non disponibile %s: %s", url, e)
        return _cache.get(key, (0, None))[1]
    _cache[key] = (time.time(), data)
    return data


def economic_events(currencies=("USD",), min_impact="High") -> list[dict]:
    data = _get_json("https://nfs.faireconomy.media/ff_calendar_thisweek.json", ttl=3 * 3600) or []
    out = []
    for e in data:
        if e.get("country") in currencies and e.get("impact") == min_impact:
            try:
                ts = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
            except (KeyError, ValueError):
                continue
            out.append({"ts": ts, "title": e.get("title", ""), "country": e["country"],
                        "forecast": e.get("forecast"), "previous": e.get("previous")})
    return sorted(out, key=lambda x: x["ts"])


def macro_blackout(cfg: dict) -> str | None:
    """Ritorna il nome dell'evento se siamo nella finestra di blackout, altrimenti None."""
    m = cfg.get("macro", {})
    now = datetime.now(timezone.utc)
    for e in economic_events(tuple(m.get("currencies", ["USD"]))):
        if e["ts"] - timedelta(minutes=m.get("blackout_before_min", 120)) <= now <= e["ts"] + timedelta(minutes=m.get("blackout_after_min", 60)):
            return f"{e['country']} {e['title']} ({e['ts']:%d/%m %H:%M} UTC)"
    return None


def upcoming_events(hours: int = 36, cfg: dict | None = None) -> list[dict]:
    now = datetime.now(timezone.utc)
    cur = tuple((cfg or {}).get("macro", {}).get("currencies", ["USD"]))
    return [e for e in economic_events(cur) if now - timedelta(hours=1) <= e["ts"] <= now + timedelta(hours=hours)]


def fear_greed() -> dict | None:
    d = _get_json("https://api.alternative.me/fng/", ttl=3600, limit=7)
    try:
        vals = [int(x["value"]) for x in d["data"]]
        return {"today": vals[0], "label": d["data"][0]["value_classification"], "week_avg": round(sum(vals) / len(vals), 1)}
    except (TypeError, KeyError, ValueError, IndexError):
        return None


def funding_rate(symbol: str) -> float | None:
    """Funding per 8h del perpetual (es. BTC/USD → BTCUSDT). >0.05% = long troppo affollati."""
    pair = symbol.split("/")[0] + "USDT"
    d = _get_json("https://fapi.binance.com/fapi/v1/premiumIndex", ttl=1800, symbol=pair)
    try:
        return float(d["lastFundingRate"])
    except (TypeError, KeyError, ValueError):
        pass
    d = _get_json("https://api.bybit.com/v5/market/tickers", ttl=1800, category="linear", symbol=pair)
    try:
        return float(d["result"]["list"][0]["fundingRate"])
    except (TypeError, KeyError, ValueError, IndexError):
        return None


def snapshot(cfg: dict) -> dict:
    return {
        "upcoming_high_impact_events": [{**e, "ts": e["ts"].isoformat()} for e in upcoming_events(48, cfg)],
        "macro_blackout_now": macro_blackout(cfg),
        "crypto_fear_greed": fear_greed(),
        "crypto_funding_8h": {s: funding_rate(s) for s in cfg["universe"]["crypto"]},
    }
