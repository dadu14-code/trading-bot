"""Test di base su dati sintetici: nessuna chiave API necessaria."""
import os
import tempfile

os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")

from tradebot import backtest, db, patterns, regime, risk, signals  # noqa: E402
from tradebot.config import load_settings  # noqa: E402

CFG = load_settings()
DF = backtest.synthetic(3000)
BARS = {"short": DF, "medium": DF.resample("4h").agg(backtest.AGG).dropna(),
        "long": DF.resample("1D").agg(backtest.AGG).dropna()}


def test_regimes():
    for f, k in ((regime.long_term, "long"), (regime.medium_term, "medium"), (regime.short_term_trigger, "short")):
        assert -1 <= f(BARS[k])["score"] <= 1


def test_analogs_no_lookahead():
    a = patterns.analogs(DF)
    assert a and a.n > 10 and 0 <= a.win_rate <= 1


def test_entry_and_veto():
    ev = signals.evaluate_entry("X", BARS, {"bias": "flat"}, CFG)
    assert not ev["go"] and any("Strategia" in v for v in ev["vetoes"])


def test_exit_trailing():
    t = {"entry_price": 100, "initial_stop": 95, "stop": 95, "highest": 100}
    reason, stop = signals.evaluate_exit(t, 112, 2, 1, "BULL", None, CFG)
    assert reason is None and stop >= 100          # pareggio dopo +1R
    reason, _ = signals.evaluate_exit({**t, "stop": stop, "highest": 112}, 99, 2, 2, "BULL", None, CFG)
    assert reason


def test_sizing_caps():
    qty, _ = risk.position_size(10_000, 100, 99, 1.0, CFG, 0, 0, True)
    assert qty * 100 <= 10_000 * CFG["risk"]["max_position_pct"] / 100 + 1e-6
    qty, _ = risk.position_size(10_000, 100, 95, 1.0, CFG, 7_950, 0, False)
    assert qty == 0


def test_circuit_breaker_kill():
    db.init()
    db.snapshot_equity(10_000, 10_000, 0)
    assert risk.circuit_breaker(8_000, CFG)["state"] == "kill"


def test_backtest_runs():
    res = backtest.run(DF, CFG, warmup=1500)
    assert "stats" in res and res["final_equity"] > 0
