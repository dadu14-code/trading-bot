"""Motore decisionale deterministico: unisce orizzonti temporali, analogie
storiche e direttiva del bot Strategia in una decisione d'ingresso/uscita.

Regole "da vecchio trader" codificate come vincoli rigidi:
  • mai comprare contro il trend primario (regime 1D BEAR)
  • mai inseguire un prezzo ipercomprato
  • se il passato in situazioni simili dice "perdi", non entrare
  • ogni ingresso ha uno stop prima ancora di essere eseguito
"""
import pandas as pd

from . import patterns, regime


def evaluate_entry(symbol: str, bars: dict[str, pd.DataFrame], directive_sym: dict | None, cfg: dict) -> dict:
    sc = cfg["signals"]
    w = sc["weights"]
    lt = regime.long_term(bars["long"])
    mt = regime.medium_term(bars["medium"])
    st = regime.short_term_trigger(bars["short"])
    an = patterns.analogs(bars["short"], k=sc["analog_k"], horizon=sc["analog_horizon_bars"])
    cp = patterns.candle_patterns(bars["short"])

    an_score = an.score if an else 0.0
    trig = st["score"] + (0.2 if cp["bullish_engulfing"] or cp["hammer"] else 0) - (0.2 if cp["bearish_engulfing"] else 0)
    norm = lambda x: (max(-1, min(1, x)) + 1) / 2
    composite = (w["long"] * norm(lt["score"]) + w["medium"] * norm(mt["score"])
                 + w["trigger"] * norm(trig) + w["analog"] * norm(an_score))

    vetoes = []
    if lt["label"] == "BEAR":
        vetoes.append("trend primario (1D) ribassista")
    if st["overextended"]:
        vetoes.append("prezzo ipercomprato (RSI>78)")
    if an and an.win_rate < sc["analog_min_winrate"]:
        vetoes.append(f"analogie storiche sfavorevoli (win {an.win_rate:.0%})")
    if st["score"] <= 0:
        vetoes.append("nessun trigger di breve (né pullback né breakout)")
    if directive_sym:
        if directive_sym.get("bias") in ("flat", "exit"):
            vetoes.append(f"bot Strategia: bias {directive_sym.get('bias')}")
    go = composite >= sc["entry_threshold"] and not vetoes

    reason = (f"1D {lt['label']}({lt['score']:+.2f}) · 4H {mt['score']:+.2f} · "
              f"trigger {'pullback' if st['pullback'] else ''}{'+' if st['pullback'] and st['breakout'] else ''}"
              f"{'breakout' if st['breakout'] else ''} · analogie "
              + (f"{an.n} casi, E[r]={an.exp_return:+.2%}, win {an.win_rate:.0%}" if an else "n/d"))
    return {"symbol": symbol, "go": go, "composite": round(composite, 3), "vetoes": vetoes,
            "reason": reason, "long": lt, "medium": mt, "short": st,
            "analog": an.as_dict() if an else None, "candles": cp, "atr_4h": mt["atr"]}


def evaluate_exit(trade: dict, price: float, atr_4h: float, bars_4h_since_entry: int,
                  long_label: str, directive_sym: dict | None, cfg: dict) -> tuple[str | None, float]:
    """Ritorna (motivo_uscita | None, nuovo_stop)."""
    rk = cfg["risk"]
    highest = max(trade["highest"], price)
    trail = highest - rk["atr_trail_mult"] * atr_4h
    # sposta lo stop solo a favore; dopo +1R porta a pareggio
    new_stop = max(trade["stop"], trail)
    one_r = trade["entry_price"] - trade["initial_stop"]
    if price - trade["entry_price"] >= one_r > 0:
        new_stop = max(new_stop, trade["entry_price"])
    if price <= new_stop:
        return "stop/trailing colpito", new_stop
    if long_label == "BEAR":
        return "trend primario girato ribassista", new_stop
    if directive_sym and directive_sym.get("bias") == "exit":
        return "il bot Strategia ordina l'uscita", new_stop
    if bars_4h_since_entry >= rk["time_stop_bars_4h"] and price < trade["entry_price"] + 0.5 * one_r:
        return "time stop: nessun progresso", new_stop
    return None, new_stop
