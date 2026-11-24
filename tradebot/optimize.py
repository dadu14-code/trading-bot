"""Ottimizzazione WALK-FORWARD dei parametri.

Perché walk-forward: ottimizzare sull'intero storico produce parametri che
"conoscono il futuro" (overfitting). Qui la storia è divisa in blocchi:
si scelgono i parametri sui blocchi passati e si misurano sul blocco
successivo, mai visto. Il risultato che conta è SOLO quello fuori campione.

Inoltre si preferiscono "altipiani": combinazioni i cui vicini funzionano
anch'essi bene (robuste), non picchi isolati (fortuna).

Uso:
  python -m tradebot.optimize --symbols BTC/USD ETH/USD SOL/USD --days 730
  python -m tradebot.optimize --synthetic
Scrive i suggerimenti in config/optimized.yaml (NON li applica da solo).
"""
import argparse
import itertools
from pathlib import Path

import numpy as np
import yaml

from . import backtest
from .config import ROOT, load_settings

GRID = {
    "entry_threshold": [0.50, 0.55, 0.60, 0.65],
    "analog_min_winrate": [0.48, 0.52, 0.56],
    "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
    "atr_trail_mult": [2.5, 3.0, 4.0],
    "time_stop_bars_4h": [20, 30, 60],
}


def objective(res: dict, min_trades: int = 4) -> float:
    """Somma degli R penalizzata per drawdown e per campioni troppo piccoli."""
    s = res["stats"]
    n = s.get("n", 0)
    if n < min_trades:
        return -1.0 + 0.01 * n
    total_r = s["avg_r"] * n
    return total_r * min(1.0, n / 15) + 20 * res["max_dd"]   # max_dd è negativo


def neighbours(combo: tuple, keys: list) -> list[tuple]:
    out = []
    for j, k in enumerate(keys):
        vals = GRID[k]
        p = vals.index(combo[j])
        for q in (p - 1, p + 1):
            if 0 <= q < len(vals):
                out.append(combo[:j] + (vals[q],) + combo[j + 1:])
    return out


def walk_forward(pres: list[tuple[dict, float]], base: dict, folds: int = 4) -> dict:
    keys = list(GRID)
    combos = list(itertools.product(*GRID.values()))
    n = len(pres[0][0]["df"])
    w0 = pres[0][0]["warmup"]
    edges = np.linspace(w0, n, folds + 2).astype(int)   # il primo blocco serve solo da training iniziale
    oos_trades, chosen, report = [], [], []

    def score(combo, a, b):
        p = {**base, **dict(zip(keys, combo))}
        return sum(objective(backtest.simulate(pre, p, fee=fee, start=a, end=b)) for pre, fee in pres)

    for f in range(1, folds + 1):
        tr_a, tr_b, te_a, te_b = edges[0], edges[f], edges[f], edges[f + 1]
        raw = {c: score(c, tr_a, tr_b) for c in combos}
        # robustezza: media del punteggio della combinazione e dei suoi vicini
        robust = {c: np.mean([raw[c]] + [raw[x] for x in neighbours(c, keys)]) for c in combos}
        best = max(robust, key=robust.get)
        p = {**base, **dict(zip(keys, best))}
        fold_trades = []
        for pre, fee in pres:
            fold_trades += backtest.simulate(pre, p, fee=fee, start=te_a, end=te_b)["trades"]
        st = backtest.trade_stats(fold_trades)
        report.append({"fold": f, "params": dict(zip(keys, best)), "oos": st})
        oos_trades += fold_trades
        chosen.append(best)
    # parametri finali: ottimizzati su tutta la storia con lo stesso criterio di robustezza
    raw = {c: score(c, edges[0], n) for c in combos}
    robust = {c: np.mean([raw[c]] + [raw[x] for x in neighbours(c, keys)]) for c in combos}
    final = dict(zip(keys, max(robust, key=robust.get)))
    cur = backtest.trade_stats(sum((backtest.simulate(pre, base, fee=fee, start=edges[1])["trades"] for pre, fee in pres), []))
    return {"folds": report, "oos_total": backtest.trade_stats(oos_trades), "current_params_same_period": cur,
            "suggested": final}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--folds", type=int, default=4)
    a = ap.parse_args()
    cfg = load_settings()
    base = backtest.params_from(cfg)
    if a.synthetic:
        datasets = [("SYN1", backtest.synthetic(6000, 7)), ("SYN2", backtest.synthetic(6000, 11))]
    else:
        syms = a.symbols or cfg["universe"]["crypto"]
        datasets = [(s, backtest.load(s, a.days)) for s in syms]
    pres = []
    for name, df in datasets:
        print(f"precalcolo segnali {name} ({len(df)} barre)…", flush=True)
        pres.append((backtest.precompute(df, cfg), backtest.FEES["crypto" if "/" in name or name.startswith("SYN") else "stock"]))
    print(f"walk-forward su {len(list(itertools.product(*GRID.values())))} combinazioni × {a.folds} blocchi…", flush=True)
    res = walk_forward(pres, base, a.folds)

    fmt = lambda s: (f"{s['n']} op · win {s['win_rate']:.0%} · {s['avg_r']:+.2f}R medio · PF {s['profit_factor']:.2f}"
                     if s.get("n") else "nessuna operazione")
    print("\nRisultati FUORI CAMPIONE per blocco:")
    for r in res["folds"]:
        print(f"  blocco {r['fold']}: {fmt(r['oos'])}  ← {r['params']}")
    print(f"\nTotale fuori campione (walk-forward): {fmt(res['oos_total'])}")
    print(f"Parametri attuali, stesso periodo:    {fmt(res['current_params_same_period'])}")
    print(f"\nParametri suggeriti: {res['suggested']}")
    out = Path(ROOT / "config" / "optimized.yaml")
    out.write_text(yaml.safe_dump({"suggested": res["suggested"], "oos_total": res["oos_total"],
                                   "current_same_period": res["current_params_same_period"],
                                   "datasets": [n for n, _ in datasets]}, allow_unicode=True, sort_keys=False))
    print(f"Salvati in {out}. Applicali in settings.yaml SOLO se il fuori-campione batte i parametri attuali.")


if __name__ == "__main__":
    main()
