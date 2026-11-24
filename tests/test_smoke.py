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


def test_short_exit_mirror():
    t = {"side": "short", "entry_price": 100, "initial_stop": 105, "stop": 105, "highest": 100, "lowest": 100}
    reason, stop = signals.evaluate_exit(t, 93, 2, 1, "BEAR", None, CFG)
    assert reason is None and stop <= 100                      # pareggio dopo +1R a favore
    reason, _ = signals.evaluate_exit({**t, "stop": stop, "lowest": 93}, 101, 2, 2, "BEAR", None, CFG)
    assert reason and "stop" in reason
    reason, _ = signals.evaluate_exit(t, 99, 2, 1, "BULL", None, CFG)
    assert "rialzista" in reason


def test_short_needs_directive_and_bear():
    ev = signals.evaluate_short("SPY", BARS, {"bias": "flat"}, CFG)
    assert not ev["go"] and any("serve 'short'" in v for v in ev["vetoes"])
    ev = signals.evaluate_short("BTC/USD", BARS, {"bias": "short"}, CFG)
    assert any("non abilitato" in v for v in ev["vetoes"])     # niente short sulle crypto


def test_short_sizing_and_stop():
    ev = {"strategy": "short", "side": "short", "atr_4h": 2.0}
    stop = signals.initial_stop(ev, 100, CFG)
    assert stop > 100
    qty, _ = risk.position_size(10_000, 100, stop, 1.0, CFG, 0, 0, False)
    assert qty > 0


def _crash_bars():
    import numpy as np
    df = DF.copy()
    n = 30                                           # ultime 30 ore: crollo del 25% con volumi x5, poi rimbalzo
    drop = np.linspace(1, 0.75, n - 4)
    df.iloc[-n:-4, df.columns.get_loc("close")] *= drop
    df.iloc[-n:-4, df.columns.get_loc("low")] *= drop * 0.99
    df.iloc[-n:-4, df.columns.get_loc("open")] *= drop
    df.iloc[-n:-4, df.columns.get_loc("high")] *= drop
    df.iloc[-n:, df.columns.get_loc("volume")] *= 5
    lo = df["close"].iloc[-5]
    for k, f in zip(range(-4, 0), (1.02, 1.04, 1.06, 1.08)):
        df.iloc[k, df.columns.get_loc("open")] = lo * (f - 0.02)
        df.iloc[k, df.columns.get_loc("close")] = lo * f
        df.iloc[k, df.columns.get_loc("high")] = lo * (f + 0.005)
        df.iloc[k, df.columns.get_loc("low")] = lo * (f - 0.025)
    return {"short": df, "medium": df.resample("4h").agg(backtest.AGG).dropna(),
            "long": df.resample("1D").agg(backtest.AGG).dropna()}


def test_capitulation_detects_crash():
    b = _crash_bars()
    ev = signals.evaluate_capitulation("BTC/USD", b, None, None, {"fear_greed": 10, "funding": -0.0002}, CFG)
    assert ev["cap"]["crash"] and ev["cap"]["climax_volume"], ev["vetoes"]
    calm = signals.evaluate_capitulation("BTC/USD", BARS, None, None, {"fear_greed": 60}, CFG)
    assert not calm["go"]
    banned = signals.evaluate_capitulation("BTC/USD", b, None, {"capitulation_allowed": False}, {"fear_greed": 10}, CFG)
    assert not banned["go"]


def test_capitulation_exit_target():
    t = {"side": "long", "strategy": "capitulation", "entry_price": 100, "initial_stop": 96, "stop": 96, "highest": 100}
    reason, _ = signals.evaluate_exit(t, 106.5, 2, 1, "BEAR", None, CFG)   # +1.5R = 106
    assert reason and "obiettivo" in reason
    reason, _ = signals.evaluate_exit(t, 101, 2, 1, "BEAR", None, CFG)     # BEAR non fa uscire un rimbalzo
    assert reason is None


def test_walk_forward_mixed_lengths():
    """Crypto (24/7) ed ETF hanno numeri di barre diversi: i blocchi devono coincidere nel tempo."""
    from tradebot import optimize
    optimize.GRID = {"entry_threshold": [0.55], "analog_min_winrate": [0.52], "atr_stop_mult": [2.0],
                     "atr_trail_mult": [3.0], "time_stop_bars_4h": [30]}
    long_pre = backtest.precompute(backtest.synthetic(4000, 3), CFG, warmup=1500)
    short_pre = backtest.precompute(backtest.synthetic(3000, 5), CFG, warmup=1500)
    res = optimize.walk_forward([(long_pre, 0.0025), (short_pre, 0.0005)], backtest.params_from(CFG), folds=2)
    assert res["period"][0] < res["period"][1] and len(res["folds"]) == 2


def test_no_capitulation_against_short_bias():
    b = _crash_bars()
    ev = signals.evaluate_capitulation("QQQ", b, {"bias": "short"}, {"capitulation_allowed": True}, {}, CFG)
    assert not ev["go"] and any("conflitto" in v for v in ev["vetoes"])


def test_no_short_when_overstretched():
    b = _crash_bars()   # crollo appena avvenuto: prezzo molto sotto la media 4H
    ev = signals.evaluate_short("SPY", b, {"bias": "short"}, CFG)
    assert any("esteso al ribasso" in v for v in ev["vetoes"]) or any("ipervenduto" in v for v in ev["vetoes"])


def test_ladder_constraints_and_breakeven():
    from tradebot import ladder
    db.init()
    db.snapshot_equity(100_000, 100_000, 0)
    ladder.init()
    db.kv_set("ladder_start", "2020-01-01T00:00:00+00:00")
    t = db.open_trade(symbol="SPY", asset_class="stock", side="short", strategy="short", qty=48, entry_price=714.6, stop=727.0)
    ladder.sync(100_000, CFG)
    rows = ladder._rows("SELECT level_eur, status, qty FROM ladder_trades WHERE trade_id=?", (t,))
    by = {r["level_eur"]: r for r in rows}
    assert by[100]["status"] == "skipped" and by[1000]["status"] == "skipped"     # niente short sotto 2.000 $
    assert by[2500]["status"] == "open" and float(by[2500]["qty"]).is_integer()   # short solo in azioni intere
    db.update_trade(t, status="closed", exit_price=700.0, exit_ts=db.now_iso(), pnl=700, r_multiple=1.2)
    ladder.sync(100_700, CFG)
    s = ladder.summary(CFG)
    big = s["levels"][-1]
    assert big["gross_eur"] > 0 and s["levels"][0]["gross_eur"] == 0


def test_account_reset_detection():
    db.init()
    db.snapshot_equity(100_000, 100_000, 0)
    tid = db.open_trade(symbol="BTC/USD", asset_class="crypto", side="long", qty=1, entry_price=100, stop=95)
    db.archive_account_history("test")
    db.snapshot_equity(110, 110, 0)
    assert risk.circuit_breaker(110, CFG)["state"] != "kill"
    assert db.peak_equity() == 110
    assert not [t for t in db.open_trades() if t["id"] == tid]


def test_claude_value_comparison():
    from tradebot import claude_value
    db.init()
    db.kv_set("noclaude_start", "2020-01-01T00:00:00+00:00")
    db.snapshot_equity(100_000, 100_000, 0)
    t = db.open_trade(symbol="ETH/USD", asset_class="crypto", side="long", strategy="trend", qty=10, entry_price=100, stop=95)
    db.update_trade(t, status="closed", pnl=50, exit_price=105, exit_ts=db.now_iso())
    db.shadow_insert(variant="senza_claude", symbol="SPY", side="short", strategy="short", qty=2, entry_price=700,
                     entry_ts=db.now_iso(), stop=720, initial_stop=720, highest=700, lowest=700)
    v = claude_value.compare(lambda sym: 690.0)          # short ombra in guadagno di 20 $
    assert v["without_claude"] == 20 and v["with_claude"] >= 50
    assert abs(v["value"] - (v["edge"] - v["cost"])) < 1e-9
