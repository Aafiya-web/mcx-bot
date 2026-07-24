"""Step 11 tests: backtest walker mechanics, cost model, metrics math,
and the hard go/no-go gate."""

import numpy as np
import pandas as pd
import pytest

from backtest.engine import (COMMISSION_PER_SIDE, compute_metrics,
                             format_report, go_no_go, run_backtest)
from config import settings
from strategies.router import get_strategy


def _history(n=1200, seed=3):
    """Synthetic 15-min history with alternating trends (like MockFeed)."""
    from data.feed import _synth_ohlcv
    return _synth_ohlcv(6000.0, n, seed)


def test_backtest_produces_closed_trades():
    trades = run_backtest(_history(), get_strategy("supertrend"),
                          "CRUDEOIL", capital=1_000_000.0)
    assert not trades.empty
    assert set(trades["exit_reason"]) <= {"STOP_LOSS", "TAKE_PROFIT",
                                          "SQUAREOFF"}
    assert (trades["lots"] >= 1).all()


def test_costs_are_charged(monkeypatch):
    # Zero slippage: a stop-loss exit must lose exactly risk + 2x commission.
    monkeypatch.setattr(settings, "PAPER_SLIPPAGE_PCT", 0.0)
    trades = run_backtest(_history(), get_strategy("supertrend"),
                          "CRUDEOIL", capital=1_000_000.0)
    stops = trades[trades["exit_reason"] == "STOP_LOSS"]
    assert not stops.empty
    for _, t in stops.iterrows():
        gross = (t["exit"] - t["entry"]) * (1 if t["side"] == "BUY" else -1) \
            * t["lots"] * 100
        assert t["pnl"] == pytest.approx(gross - 2 * COMMISSION_PER_SIDE)


def test_metrics_on_known_trades():
    trades = pd.DataFrame({
        "pnl": [1000.0, -500.0, 2000.0, -500.0],
        "exit_reason": ["TAKE_PROFIT", "STOP_LOSS", "TAKE_PROFIT",
                        "STOP_LOSS"],
    })
    m = compute_metrics(trades, 100_000)
    assert m["total_trades"] == 4
    assert m["win_rate"] == pytest.approx(50.0)
    assert m["profit_factor"] == pytest.approx(3.0)
    assert m["total_pnl"] == pytest.approx(2000.0)
    assert m["expectancy"] == pytest.approx(500.0)
    assert m["max_drawdown"] < 0


def test_go_no_go_gate():
    passing = {"profit_factor": 1.8, "max_drawdown": -6.0, "win_rate": 54.0,
               "total_trades": 150}
    assert go_no_go(passing)["go"] is True

    for field, bad in [("profit_factor", 1.4), ("max_drawdown", -12.0),
                       ("win_rate", 48.0), ("total_trades", 60)]:
        failing = dict(passing, **{field: bad})
        result = go_no_go(failing)
        assert result["go"] is False
        assert any(name == field and not ok
                   for name, ok, _ in result["criteria"])


def test_no_trades_is_no_go():
    assert go_no_go({"error": "No trades generated"})["go"] is False


def test_report_contains_verdict():
    metrics = {"profit_factor": 1.8, "max_drawdown": -6.0, "win_rate": 54.0,
               "total_trades": 150, "total_pnl": 50_000.0,
               "return_pct": 10.0, "sharpe_ratio": 1.2,
               "expectancy": 333.0, "avg_win": 1.0, "avg_loss": -1.0,
               "by_exit": {}}
    text = format_report("CRUDEOIL", "supertrend", metrics, 500_000)
    assert "✅ GO" in text
    metrics["win_rate"] = 40.0
    assert "❌ NO-GO" in format_report("CRUDEOIL", "supertrend", metrics,
                                       500_000)


def _gold_history_with_cross(sessions=170, seed=11):
    """15-min GOLD bars whose hourly 50/200 EMA crosses AFTER the 3x200
    convergence threshold (~600 hourly bars). Long decline, then rally: the
    death->golden cross must land past bar 600 or the strategy (correctly)
    holds on warmup. ~34 contiguous bars/'session' -> 1 hourly bar per 4."""
    import numpy as np
    import pandas as pd
    per = 34
    n = sessions * per                       # ~5780 15-min -> ~1445 hourly
    turn = int(n * 0.62)                     # cross lands well past hourly 600
    base = np.concatenate([np.linspace(152000, 146000, turn),
                           np.linspace(146000, 158000, n - turn)])
    close = pd.Series(base, index=pd.date_range(
        "2026-03-01 09:00", periods=n, freq="15min"))
    return pd.DataFrame({"open": close.shift(1).fillna(close.iloc[0]),
                         "high": close + 30, "low": close - 30,
                         "close": close, "volume": 500.0})


def test_hourly_strategy_detects_cross_after_convergence_fix():
    """Regression for the 2026-07-24 bug: GOLD (ema_trend, 1H) generated
    ZERO trades over 180 days because the per-step window never held enough
    hourly bars for the EMA-200 to converge. With the full-history 1H
    precompute, a real crossing must now produce at least one trade."""
    df = _gold_history_with_cross()
    trades = run_backtest(df, get_strategy("ema_trend"), "GOLDM",
                          capital=5_000_000.0)   # big cap so sizing allows it
    assert not trades.empty, "converged EMA must find the crossing"


def test_backtest_h1_gate_has_no_lookahead():
    """The hourly series handed to the strategy at bar i must never contain
    a bar that has not fully completed by then."""
    import pandas as pd
    from backtest.engine import _H1_AGG, _H1_COMPLETE_LAG
    df = _gold_history_with_cross(sessions=30)
    h1_full = df.resample("1h").agg(_H1_AGG).dropna()
    for i in (100, 300, 600, len(df) - 1):
        ts_i = df.index[i]
        h1 = h1_full[h1_full.index <= ts_i - _H1_COMPLETE_LAG]
        if len(h1):
            # last visible hourly bar must have fully closed by bar i's close
            assert h1.index[-1] + pd.Timedelta(hours=1) <= \
                ts_i + pd.Timedelta(minutes=15)
