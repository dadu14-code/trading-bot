"""Motore decisionale deterministico. Tre strategie, ognuna con i suoi veti rigidi:

  TREND LONG   – compra i pullback/breakout in trend rialzista confermato su più orizzonti
  SHORT (ETF)  – specchio del trend long: vende i rimbalzi falliti in trend ribassista
  RIMBALZO     – capitolazione: crollo esteso + volume climax + prima inversione;
                 rischio piccolo, obiettivo vicino, uscita rapida

Regole "da vecchio trader" valide per tutte:
  • mai contro il trend primario (tranne il rimbalzo, che ha rischio ridotto e uscita rapida)
  • mai inseguire un prezzo esteso (ipercomprato per i long, ipervenduto per gli short)
  • se il passato in situazioni simili dice "perdi", non entrare
  • ogni ingresso ha uno stop prima ancora di essere eseguito
"""
import pandas as pd

from . import indicators as ind
from . import patterns, regime

_norm = lambda x: (max(-1, min(1, x)) + 1) / 2


def _analog_text(an) -> str:
    return f"{an.n} casi, E[r]={an.exp_return:+.2%}, win {an.win_rate:.0%}" if an else "n/d"


def evaluate_entry(symbol: str, bars: dict[str, pd.DataFrame], directive_sym: dict | None, cfg: dict) -> dict:
    """TREND LONG."""
    sc = cfg["signals"]
    w = sc["weights"]
    lt = regime.long_term(bars["long"])
    mt = regime.medium_term(bars["medium"])
    st = regime.short_term_trigger(bars["short"])
    an = patterns.analogs(bars["short"], k=sc["analog_k"], horizon=sc["analog_horizon_bars"])
    cp = patterns.candle_patterns(bars["short"])

    trig = st["score"] + (0.2 if cp["bullish_engulfing"] or cp["hammer"] else 0) - (0.2 if cp["bearish_engulfing"] else 0)
    composite = (w["long"] * _norm(lt["score"]) + w["medium"] * _norm(mt["score"])
                 + w["trigger"] * _norm(trig) + w["analog"] * _norm(an.score if an else 0))
    vetoes = []
    if lt["label"] == "BEAR":
        vetoes.append("trend primario (1D) ribassista")
    if st["overextended"]:
        vetoes.append("prezzo ipercomprato (RSI>78)")
    if an and an.win_rate < sc["analog_min_winrate"]:
        vetoes.append(f"analogie storiche sfavorevoli (win {an.win_rate:.0%})")
    if st["score"] <= 0:
        vetoes.append("nessun trigger di breve (né pullback né breakout)")
    if directive_sym and directive_sym.get("bias") in ("flat", "exit", "short"):
        vetoes.append(f"bot Strategia: bias {directive_sym.get('bias')}")
    go = composite >= sc["entry_threshold"] and not vetoes
    trig_txt = "+".join(n for n, v in (("pullback", st["pullback"]), ("breakout", st["breakout"])) if v) or "-"
    reason = f"1D {lt['label']}({lt['score']:+.2f}) · 4H {mt['score']:+.2f} · trigger {trig_txt} · analogie {_analog_text(an)}"
    return {"symbol": symbol, "strategy": "trend", "side": "long", "go": go, "composite": round(composite, 3),
            "vetoes": vetoes, "reason": reason, "long": lt, "medium": mt, "short": st,
            "analog": an.as_dict() if an else None, "candles": cp, "atr_4h": mt["atr"]}


def evaluate_short(symbol: str, bars: dict[str, pd.DataFrame], directive_sym: dict | None, cfg: dict) -> dict:
    """SHORT su ETF: richiede trend ribassista su 1D e 4H e il consenso esplicito del bot Strategia."""
    sc, sh = cfg["signals"], cfg.get("shorts", {})
    w = sc["weights"]
    lt = regime.long_term(bars["long"])
    mt = regime.medium_term(bars["medium"])
    st = regime.short_term_trigger_short(bars["short"])
    an = patterns.analogs(bars["short"], k=sc["analog_k"], horizon=sc["analog_horizon_bars"])
    cp = patterns.candle_patterns(bars["short"])
    trig = st["score"] + (0.2 if cp["bearish_engulfing"] or cp["shooting_star"] else 0) - (0.2 if cp["bullish_engulfing"] or cp["hammer"] else 0)
    composite = (w["long"] * _norm(-lt["score"]) + w["medium"] * _norm(-mt["score"])
                 + w["trigger"] * _norm(trig) + w["analog"] * _norm(-(an.score if an else 0)))
    vetoes = []
    if not sh.get("enabled", False) or symbol not in sh.get("symbols", []):
        vetoes.append("short non abilitato su questo strumento")
    if lt["label"] != "BEAR":
        vetoes.append(f"trend primario (1D) non ribassista ({lt['label']})")
    if mt["score"] > -0.2:
        vetoes.append("trend intermedio (4H) non ribassista")
    if st["oversold"]:
        vetoes.append("prezzo ipervenduto (RSI<22): rischio rimbalzo violento")
    h4 = bars["medium"]
    stretch = float((ind.ema(h4["close"], 20).iloc[-1] - h4["close"].iloc[-1]) / mt["atr"]) if mt["atr"] else 0
    if stretch > sh.get("max_stretch_atr", 3.0):
        vetoes.append(f"prezzo già esteso al ribasso ({stretch:.1f} ATR sotto la media 4H): rischio squeeze, attendo il rimbalzo")
    if an and (1 - an.win_rate) < sc["analog_min_winrate"]:
        vetoes.append(f"analogie storiche sfavorevoli allo short (ribasso nel {1 - an.win_rate:.0%} dei casi)")
    if st["score"] <= 0:
        vetoes.append("nessun trigger short (né rimbalzo fallito né rottura dei minimi)")
    if directive_sym is not None and directive_sym.get("bias") != "short":
        vetoes.append(f"bot Strategia: bias {directive_sym.get('bias')} (serve 'short')")
    go = composite >= sh.get("entry_threshold", sc["entry_threshold"]) and not vetoes
    trig_txt = "+".join(n for n, v in (("rimbalzo fallito", st["failed_rally"]), ("rottura minimi", st["breakdown"])) if v) or "-"
    reason = f"SHORT · 1D {lt['label']}({lt['score']:+.2f}) · 4H {mt['score']:+.2f} · trigger {trig_txt} · analogie {_analog_text(an)}"
    return {"symbol": symbol, "strategy": "short", "side": "short", "go": go, "composite": round(composite, 3),
            "vetoes": vetoes, "reason": reason, "long": lt, "medium": mt, "short": st,
            "analog": an.as_dict() if an else None, "candles": cp, "atr_4h": mt["atr"]}


def evaluate_capitulation(symbol: str, bars: dict[str, pd.DataFrame], directive_sym: dict | None,
                          directive: dict | None, sentiment: dict, cfg: dict) -> dict:
    """RIMBALZO da capitolazione: tutte e tre le condizioni (crollo, volume climax, inversione)."""
    cc = cfg.get("capitulation", {})
    cap = regime.capitulation(bars["long"], bars["medium"], bars["short"])
    crypto = "/" in symbol
    fg = sentiment.get("fear_greed")
    fund = sentiment.get("funding")
    extras = []
    if crypto and fg is not None and fg <= cc.get("max_fear_greed", 20):
        extras.append(f"Fear&Greed {fg}")
    if crypto and fund is not None and fund < 0:
        extras.append(f"funding negativo {fund:+.4%}")
    if not crypto and cap["rsi_daily_min3"] < 30:
        extras.append(f"RSI giornaliero {cap['rsi_daily_min3']}")
    score = (0.3 * cap["crash"] + 0.3 * cap["climax_volume"] + 0.25 * cap["reversal"] + 0.075 * min(len(extras), 2))
    vetoes = []
    if not cc.get("enabled", False):
        vetoes.append("modulo rimbalzo disattivato")
    for k, label in (("crash", "nessun crollo esteso"), ("climax_volume", "manca il volume di capitolazione"),
                     ("reversal", "nessun segnale di inversione")):
        if not cap[k]:
            vetoes.append(label)
    if crypto and fg is not None and fg > cc.get("max_fear_greed", 20) + 15:
        vetoes.append(f"nessuna paura estrema (Fear&Greed {fg})")
    if directive and directive.get("capitulation_allowed") is False:
        vetoes.append("bot Strategia: rimbalzi non consentiti")
    if directive_sym and directive_sym.get("bias") in ("exit", "short"):
        vetoes.append(f"bot Strategia: bias {directive_sym.get('bias')} su questo asset (niente rimbalzi in conflitto)")
    go = score >= cc.get("min_score", 0.85) and not vetoes
    reason = (f"RIMBALZO · {cap['stretch_atr']:.1f} ATR sotto la media 4H · volume x{cap['volume_vs_avg']:.1f} · "
              f"{', '.join(cap['signals']) or 'nessuna inversione'}" + (f" · {', '.join(extras)}" if extras else ""))
    return {"symbol": symbol, "strategy": "capitulation", "side": "long", "go": go, "composite": round(score, 3),
            "vetoes": vetoes, "reason": reason, "cap": cap, "atr_4h": cap["atr_4h"], "cap_low": cap["cap_low"]}


def initial_stop(ev: dict, price: float, cfg: dict) -> float:
    rk = cfg["risk"]
    if ev["strategy"] == "capitulation":   # sotto il minimo della capitolazione
        return min(ev["cap_low"] - cfg["capitulation"].get("stop_buffer_atr", 0.5) * ev["atr_4h"],
                   price - 0.5 * ev["atr_4h"])
    m = cfg.get("shorts", {}).get("atr_stop_mult", rk["atr_stop_mult"]) if ev["side"] == "short" else rk["atr_stop_mult"]
    return price + m * ev["atr_4h"] if ev["side"] == "short" else price - m * ev["atr_4h"]


def evaluate_exit(trade: dict, price: float, atr_4h: float, bars_4h_since_entry: int,
                  long_label: str, directive_sym: dict | None, cfg: dict) -> tuple[str | None, float]:
    """Ritorna (motivo_uscita | None, nuovo_stop). Lo stop si muove solo a favore."""
    rk = cfg["risk"]
    side = trade.get("side") or "long"
    strat = trade.get("strategy") or "trend"
    entry, init = trade["entry_price"], trade["initial_stop"]
    one_r = abs(entry - init)
    bias = (directive_sym or {}).get("bias")

    if side == "short":
        lowest = min(trade.get("lowest") or entry, price)
        trail_mult = cfg.get("shorts", {}).get("atr_trail_mult", rk["atr_trail_mult"])
        new_stop = min(trade["stop"], lowest + trail_mult * atr_4h)
        if entry - price >= one_r > 0:
            new_stop = min(new_stop, entry)
        if price >= new_stop:
            return "stop/trailing colpito", new_stop
        if long_label == "BULL":
            return "trend primario girato rialzista", new_stop
        if bias in ("exit", "long"):
            return f"il bot Strategia ordina l'uscita (bias {bias})", new_stop
        if bars_4h_since_entry >= rk["time_stop_bars_4h"] and price > entry - 0.5 * one_r:
            return "time stop: nessun progresso", new_stop
        return None, new_stop

    highest = max(trade["highest"], price)
    if strat == "capitulation":
        cc = cfg["capitulation"]
        new_stop = trade["stop"]
        if price - entry >= one_r > 0:
            new_stop = max(new_stop, entry)                        # pareggio dopo +1R
        if price <= new_stop:
            return "stop colpito", new_stop
        if price >= entry + cc.get("target_r", 1.5) * one_r:
            return f"obiettivo +{cc.get('target_r', 1.5)}R raggiunto", new_stop
        if bars_4h_since_entry >= cc.get("time_stop_bars_4h", 12):
            return "time stop rimbalzo", new_stop
        if bias == "exit":
            return "il bot Strategia ordina l'uscita", new_stop
        return None, new_stop

    new_stop = max(trade["stop"], highest - rk["atr_trail_mult"] * atr_4h)
    if price - entry >= one_r > 0:
        new_stop = max(new_stop, entry)
    if price <= new_stop:
        return "stop/trailing colpito", new_stop
    if long_label == "BEAR":
        return "trend primario girato ribassista", new_stop
    if bias in ("exit", "short"):
        return f"il bot Strategia ordina l'uscita (bias {bias})", new_stop
    if bars_4h_since_entry >= rk["time_stop_bars_4h"] and price < entry + 0.5 * one_r:
        return "time stop: nessun progresso", new_stop
    return None, new_stop
