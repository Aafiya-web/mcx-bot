"""Step 9 tests: session clock, kill switches, squareoff, and the
end-to-end mock session."""

from datetime import datetime

import pytest

from broker.order_manager import PaperExecutor
from core import scheduler
from core.engine import Engine
from data.feed import MockFeed
from database import models


# --------------------------------------------------------------- scheduler


def test_market_hours():
    assert scheduler.is_market_open(datetime(2026, 7, 6, 10, 0))    # Mon
    assert not scheduler.is_market_open(datetime(2026, 7, 6, 8, 30))
    assert not scheduler.is_market_open(datetime(2026, 7, 6, 23, 45))
    assert not scheduler.is_market_open(datetime(2026, 7, 5, 12, 0))  # Sun


def test_entry_and_squareoff_windows():
    assert scheduler.entries_allowed(datetime(2026, 7, 6, 10, 0))
    assert not scheduler.entries_allowed(datetime(2026, 7, 6, 23, 5))
    assert not scheduler.in_squareoff_zone(datetime(2026, 7, 6, 22, 0))
    assert scheduler.in_squareoff_zone(datetime(2026, 7, 6, 23, 20))


# ------------------------------------------------------------------ engine


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr("notifications.telegram.send_message",
                        lambda *a, **k: True)
    db = tmp_path / "engine.db"
    feed = MockFeed(symbols=["CRUDEOIL", "GOLD"], n_bars=400, seed=42)
    return Engine(feed, PaperExecutor(feed.get_ltp), db_path=db,
                  capital=1_000_000.0, symbols=["CRUDEOIL", "GOLD"])


def test_engine_tick_runs_clean(engine):
    engine.tick(engine.feed.now)
    assert engine.stats["ticks"] == 1


def test_halt_flag_stops_everything(engine, monkeypatch):
    models.set_halted(True, engine.db)
    scans = {"n": 0}
    monkeypatch.setattr(engine, "_scan_for_entries",
                        lambda now: scans.__setitem__("n", scans["n"] + 1))
    engine.tick(datetime(2026, 7, 6, 10, 0))
    assert scans["n"] == 0  # halted: no scanning, ever


def test_pause_flag_blocks_new_entries_only(engine, monkeypatch):
    models.set_state("paused", "1", engine.db)
    scans = {"n": 0}
    monkeypatch.setattr(engine, "_scan_for_entries",
                        lambda now: scans.__setitem__("n", scans["n"] + 1))
    engine.tick(datetime(2026, 7, 6, 10, 0))
    assert scans["n"] == 0
    models.set_state("paused", "0", engine.db)
    # next 15-min bar (scans happen once per bar close — landmine L1)
    engine.tick(datetime(2026, 7, 6, 10, 15))
    assert scans["n"] == 1


def test_no_entries_outside_market_hours(engine, monkeypatch):
    scans = {"n": 0}
    monkeypatch.setattr(engine, "_scan_for_entries",
                        lambda now: scans.__setitem__("n", scans["n"] + 1))
    engine.tick(datetime(2026, 7, 6, 8, 0))    # pre-open
    engine.tick(datetime(2026, 7, 4, 12, 0))   # Saturday
    assert scans["n"] == 0


def test_squareoff_flattens_open_positions(engine):
    from strategies.base import Signal
    sig = Signal("BUY", "test", 6000.0, 5900.0, 6300.0, 2.0, 60.0, "t")
    engine.monitor.open_position("CRUDEOIL", sig, 1)
    assert len(models.get_open_positions(engine.db)) == 1
    engine.tick(datetime(2026, 7, 6, 23, 20))
    assert models.get_open_positions(engine.db) == []
    assert engine.stats["closes"] == 1


def test_full_mock_session_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr("notifications.telegram.send_message",
                        lambda *a, **k: True)
    db = tmp_path / "session.db"
    feed = MockFeed(n_bars=900, seed=42)  # all five instruments
    engine = Engine(feed, PaperExecutor(feed.get_ltp), db_path=db,
                    capital=500_000.0)
    summary = engine.run_mock_session()

    assert summary["ticks"] > 500
    assert summary["candidates"] >= 1            # strategies produced signals
    assert summary["approved"] <= summary["candidates"]
    # squareoff discipline: nothing survives the session end
    assert models.get_open_positions(db) == []
    # the agentic layer wrote its audit trail
    with models._conn(db) as c:
        n = c.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0]
    assert n > 0


def test_engine_equity_tracks_open_position(engine):
    from strategies.base import Signal
    start = engine.equity
    engine.feed.set_ltp("CRUDEOIL", 6000.0)
    sig = Signal("BUY", "test", 6000.0, 5900.0, 6300.0, 2.0, 60.0, "t")
    engine.monitor.open_position("CRUDEOIL", sig, 1)
    engine.feed.set_ltp("CRUDEOIL", 6050.0)
    # +50 points x 1 lot x 100 point value, minus entry slippage cost
    assert engine.equity > start
    assert engine.unrealized() == pytest.approx(
        (6050.0 - 6000.0 * 1.0005) * 100, rel=1e-6)
