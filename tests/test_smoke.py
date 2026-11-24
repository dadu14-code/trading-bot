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
    assert "stats" in res and res["curve"][-1] > 0


def test_cooldown_8h():
    import time
    db.init()
    for i in range(3):
        tid = db.open_trade(symbol="X", asset_class="crypto", side="long", qty=1, entry_price=100, stop=95)
        db.update_trade(tid, status="closed", pnl=-5, exit_ts=db.now_iso(), r_multiple=-1)
    db.kv_set("cooldown_until", 0)
    cb = risk.circuit_breaker(10_000, CFG)
    hrs = (db.kv_get("cooldown_until") - time.time()) / 3600
    assert cb["state"] == "cooldown" and 7.9 < hrs <= 8.0
    db.kv_set("cooldown_until", 0)


def test_macro_blackout(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from tradebot import macro
    soon = datetime.now(timezone.utc) + timedelta(minutes=30)
    monkeypatch.setattr(macro, "economic_events", lambda *a, **k: [{"ts": soon, "title": "CPI m/m", "country": "USD"}])
    assert "CPI" in macro.macro_blackout(CFG)
    later = datetime.now(timezone.utc) + timedelta(hours=5)
    monkeypatch.setattr(macro, "economic_events", lambda *a, **k: [{"ts": later, "title": "CPI m/m", "country": "USD"}])
    assert macro.macro_blackout(CFG) is None


def test_vol_scaler_and_merge():
    from tradebot.config import deep_merge
    m = deep_merge(CFG, {"risk": {"atr_stop_mult": 9}})
    assert m["risk"]["atr_stop_mult"] == 9 and m["risk"]["max_open_positions"] == CFG["risk"]["max_open_positions"]
    mult, _ = risk.vol_scaler(CFG)
    assert 0 < mult <= 1


def test_walk_forward_small():
    from tradebot import optimize
    optimize.GRID = {"entry_threshold": [0.5, 0.6], "analog_min_winrate": [0.5], "atr_stop_mult": [2.0],
                     "atr_trail_mult": [3.0], "time_stop_bars_4h": [30]}
    pre = backtest.precompute(DF, CFG, warmup=1500)
    res = optimize.walk_forward([(pre, 0.0025)], backtest.params_from(CFG), folds=2)
    assert len(res["folds"]) == 2 and "suggested" in res
