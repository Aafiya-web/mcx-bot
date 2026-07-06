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
