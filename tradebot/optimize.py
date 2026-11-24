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
    """I blocchi sono divisi per DATA (non per numero di barre): crypto (24/7) ed ETF
    (solo orari di borsa) hanno quantità di barre diverse ma gli stessi periodi."""
    keys = list(GRID)
    combos = list(itertools.product(*GRID.values()))
    t0 = max(pre["df"].index[pre["warmup"]] for pre, _ in pres)
    t1 = min(pre["df"].index[-1] for pre, _ in pres)
    tedges = [t0 + (t1 - t0) * k / (folds + 1) for k in range(folds + 2)]  # il primo blocco è solo training
    idx = [[int(pre["df"].index.searchsorted(t)) for t in tedges] for pre, _ in pres]
    for row, (pre, _) in zip(idx, pres):
        row[-1] = len(pre["df"])

    def sims(params, a, b):  # a, b = numero del bordo temporale
        return [backtest.simulate(pre, params, fee=fee, start=idx[j][a], end=idx[j][b]) for j, (pre, fee) in enumerate(pres)]

    def score(combo, a, b):
        return sum(objective(r) for r in sims({**base, **dict(zip(keys, combo))}, a, b))

    def pick(a, b):
        raw = {c: score(c, a, b) for c in combos}
        # robustezza: media del punteggio della combinazione e dei suoi vicini
        robust = {c: np.mean([raw[c]] + [raw[x] for x in neighbours(c, keys)]) for c in combos}
        return max(robust, key=robust.get)

    oos_trades, report = [], []
    for f in range(1, folds + 1):
        best = pick(0, f)
        fold_trades = sum((r["trades"] for r in sims({**base, **dict(zip(keys, best))}, f, f + 1)), [])
        report.append({"fold": f, "from": str(tedges[f].date()), "to": str(tedges[f + 1].date()),
                       "params": dict(zip(keys, best)), "oos": backtest.trade_stats(fold_trades)})
        oos_trades += fold_trades
    final = dict(zip(keys, pick(0, folds + 1)))   # parametri finali: tutta la storia, stesso criterio
    cur = backtest.trade_stats(sum((r["trades"] for r in sims(base, 1, folds + 1)), []))
    return {"folds": report, "oos_total": backtest.trade_stats(oos_trades), "current_params_same_period": cur,
            "suggested": final, "period": [str(tedges[1].date()), str(t1.date())]}


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
