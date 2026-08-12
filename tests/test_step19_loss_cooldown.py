"""Step 19 — per-instrument loss-streak cooldown.

Cross-day complement to the intraday account-wide 3-loss veto: after N
consecutive LOSING closes on one instrument it is benched for a cooldown
window (others keep trading; auto-resumes). Risk-tightening only.
Motivated by the 2026-08 NATGASMINI multi-day bleed.
"""

from datetime import datetime, timedelta

import pytest

from broker.order_manager import PaperExecutor
from config import settings
from core.engine import Engine
from data.feed import MockFeed
from database import models
from positions.monitor import CloseEvent


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr("notifications.telegram.send_message",
                        lambda *a, **k: True)
    monkeypatch.setattr(settings, "LOSS_STREAK_LIMIT", 3)
    monkeypatch.setattr(settings, "LOSS_STREAK_COOLDOWN_DAYS", 2)
    db = tmp_path / "cooldown.db"
    feed = MockFeed(symbols=["CRUDEOIL", "GOLD"], n_bars=400, seed=42)
    return Engine(feed, PaperExecutor(feed.get_ltp), db_path=db,
                  capital=1_000_000.0, symbols=["CRUDEOIL", "GOLD"])


def _seed_closed(db, symbol, pnls):
    """Insert CLOSED trades with given P&Ls, oldest first."""
    base = datetime(2026, 8, 6, 10, 0)
    with models._conn(db) as c:
        for k, pnl in enumerate(pnls):
            c.execute(
                "INSERT INTO trades (symbol, side, qty, entry_price, "
                "stop_loss, take_profit, strategy, status, pnl, exit_time) "
                "VALUES (?,?,?,?,?,?,?,'CLOSED',?,?)",
                (symbol, "BUY", 1, 100.0, 99.0, 102.0, "test", pnl,
                 (base + timedelta(hours=k)).isoformat()))


def _close_ev(symbol, pnl):
    return CloseEvent(trade_id=1, symbol=symbol, side="BUY", qty=1,
                      exit_reason="STOP_LOSS", intended_price=99.0,
                      fill_price=98.5, pnl=pnl)


def test_streak_trips_cooldown(engine):
    _seed_closed(engine.db, "CRUDEOIL", [-100, -200, -300])
    engine._on_close(_close_ev("CRUDEOIL", -300))
    until = models.get_state("symbol_cooldown:CRUDEOIL", "", engine.db)
    assert until  # benched
    with models._conn(engine.db) as c:
        row = c.execute("SELECT decision FROM decision_log WHERE "
                        "symbol='CRUDEOIL' AND stage='system'").fetchone()
    assert row and row[0] == "COOLDOWN"


def test_win_in_window_prevents_cooldown(engine):
    _seed_closed(engine.db, "CRUDEOIL", [-100, 500, -300])  # a win between
    engine._on_close(_close_ev("CRUDEOIL", -300))
    assert models.get_state("symbol_cooldown:CRUDEOIL", "", engine.db) == ""


def test_two_losses_not_enough(engine):
    _seed_closed(engine.db, "CRUDEOIL", [-100, -200])
    engine._on_close(_close_ev("CRUDEOIL", -200))
    assert models.get_state("symbol_cooldown:CRUDEOIL", "", engine.db) == ""


def test_benched_symbol_skipped_others_scan(engine):
    import json
    # cooldown dates are relative to the tick's `now`, not the wall clock
    models.set_state("symbol_cooldown:CRUDEOIL", "2026-07-08", engine.db)
    engine.tick(datetime(2026, 7, 6, 11, 0))   # 07-06 < 07-08 -> benched
    snap = json.loads(models.get_state("scan_snapshot", "{}", engine.db))
    stat = {r["symbol"]: r["status"] for r in snap["rows"]}
    assert stat["CRUDEOIL"].startswith("cooldown:")
    assert not stat["GOLD"].startswith("cooldown:")   # others unaffected


def test_auto_lift_after_window(engine):
    # resume-date 07-05 is before the tick's now (07-06) -> expired
    models.set_state("symbol_cooldown:CRUDEOIL", "2026-07-05", engine.db)
    engine._maintenance_date = None       # force the daily-maintenance branch
    engine.tick(datetime(2026, 7, 6, 11, 0))
    assert models.get_state("symbol_cooldown:CRUDEOIL", "", engine.db) == ""
    with models._conn(engine.db) as c:
        decs = [r[0] for r in c.execute(
            "SELECT decision FROM decision_log WHERE symbol='CRUDEOIL'")]
    assert "RESUME" in decs


def test_limit_zero_disables(engine, monkeypatch):
    monkeypatch.setattr(settings, "LOSS_STREAK_LIMIT", 0)
    _seed_closed(engine.db, "CRUDEOIL", [-100, -200, -300, -400])
    engine._on_close(_close_ev("CRUDEOIL", -400))
    assert models.get_state("symbol_cooldown:CRUDEOIL", "", engine.db) == ""


def test_recent_closed_pnls_helper(engine):
    _seed_closed(engine.db, "GOLD", [-1, -2, -3, 4, -5])  # newest = -5
    got = models.recent_closed_pnls("GOLD", 3, engine.db)
    assert got == [-5, 4, -3]     # newest first


def test_backtest_cooldown_halts_entries_on_all_loss_series(monkeypatch):
    """The backtest walker mirrors the live cooldown: a relentless
    loss-maker stops taking entries after the limit."""
    import numpy as np
    import pandas as pd

    from backtest.engine import run_backtest
    from strategies.base import Signal, Strategy

    monkeypatch.setattr(settings, "LOSS_STREAK_LIMIT", 3)
    monkeypatch.setattr(settings, "LOSS_STREAK_COOLDOWN_DAYS", 2)

    # a strategy that always goes long with a tight stop that always hits
    class AlwaysLong(Strategy):
        name = "always"

        def generate(self, df15, df1h, regime, now):
            px = float(df15["close"].iloc[-1])
            return Signal("BUY", "always", px, px - 5, px + 10, 2.0, 3.0, "t")

    idx = pd.date_range("2026-07-01 09:00", periods=600, freq="15min",
                        tz="Asia/Kolkata")
    # gently falling market so every long stops out
    close = pd.Series(np.linspace(6000, 5400, len(idx)), index=idx)
    df = pd.DataFrame({"open": close, "high": close + 1, "low": close - 8,
                       "close": close - 6, "volume": 100.0}, index=idx)

    monkeypatch.setattr(settings, "LOSS_STREAK_LIMIT", 0)
    trades_off = run_backtest(df, AlwaysLong(), "CRUDEOIL", capital=1e6)
    monkeypatch.setattr(settings, "LOSS_STREAK_LIMIT", 3)
    trades_on = run_backtest(df, AlwaysLong(), "CRUDEOIL", capital=1e6)

    assert len(trades_on) < len(trades_off)   # cooldown suppressed entries
