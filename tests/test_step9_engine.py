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


def test_scan_snapshot_written(engine):
    import json
    engine.tick(datetime(2026, 7, 6, 11, 0))
    snap = json.loads(models.get_state("scan_snapshot", "{}", engine.db))
    assert {r["symbol"] for r in snap["rows"]} == {"CRUDEOIL", "GOLD"}
    assert all(r["status"] for r in snap["rows"])   # every verdict worded


def test_watch_mode_exit_detail_decrements_and_disables(tmp_path,
                                                        monkeypatch):
    sent = []
    monkeypatch.setattr("notifications.telegram.send_message",
                        lambda t, *a, **k: sent.append(t) or True)
    db = tmp_path / "watch.db"
    feed = MockFeed(symbols=["CRUDEOIL", "GOLD"], n_bars=400, seed=42)
    eng = Engine(feed, PaperExecutor(feed.get_ltp), db_path=db,
                 capital=1_000_000.0, symbols=["CRUDEOIL", "GOLD"])
    models.set_state("watch_mode_remaining", "1", db)

    from strategies.base import Signal
    sig = Signal("BUY", "test", 6000.0, 5900.0, 6300.0, 2.0, 60.0, "t")
    feed.set_ltp("CRUDEOIL", 6000.0)
    tid = eng.monitor.open_position("CRUDEOIL", sig, 1)
    ev = eng.monitor.close_position(tid, "SQUAREOFF")
    eng._on_close(ev)

    assert any("WATCH — exit detail" in t for t in sent)
    assert any("watch mode complete" in t for t in sent)
    assert models.get_state("watch_mode_remaining", "", db) == "0"

    sent.clear()                       # disarmed: next close is terse
    tid = eng.monitor.open_position("CRUDEOIL", sig, 1)
    eng._on_close(eng.monitor.close_position(tid, "SQUAREOFF"))
    assert not any("WATCH" in t for t in sent)


def test_watch_mode_entry_detail_includes_gate_chain(tmp_path, monkeypatch):
    from types import SimpleNamespace

    sent = []
    monkeypatch.setattr("notifications.telegram.send_message",
                        lambda t, *a, **k: sent.append(t) or True)
    db = tmp_path / "watch2.db"
    feed = MockFeed(symbols=["CRUDEOIL"], n_bars=200, seed=1)
    eng = Engine(feed, PaperExecutor(feed.get_ltp), db_path=db,
                 capital=1_000_000.0, symbols=["CRUDEOIL"])

    from strategies.base import Signal
    sig = Signal("BUY", "test", 6000.0, 5900.0, 6200.0, 2.0, 60.0, "t")
    decision = SimpleNamespace(stages={"risk_team": SimpleNamespace(
        fields={"gate": [("regime", True, "TRENDING ok"),
                         ("correlation", True, "no cluster clash")]})})
    eng._watch_entry_detail("CRUDEOIL", sig, decision)

    assert any("WATCH — entry detail" in t for t in sent)
    body = next(t for t in sent if "entry detail" in t)
    assert "✅ regime" in body and "✅ correlation" in body
    assert "R unit" in body


def test_symbol_pause_skips_scanning(engine, monkeypatch):
    import json
    models.set_state("symbol_pause:CRUDEOIL", "expiring JUL contract",
                     engine.db)
    seen = []
    real = engine.feed.get_candles
    monkeypatch.setattr(
        engine.feed, "get_candles",
        lambda s, i="FIFTEEN_MINUTE", lb=200: (seen.append(s)
                                               or real(s, i, lb)))
    engine.tick(datetime(2026, 7, 6, 11, 0))
    assert "CRUDEOIL" not in seen          # never fetched, never scanned
    assert "GOLD" in seen                  # others unaffected
    snap = json.loads(models.get_state("scan_snapshot", "{}", engine.db))
    stat = {r["symbol"]: r["status"] for r in snap["rows"]}
    assert stat["CRUDEOIL"].startswith("paused: expiring JUL")


def test_pause_auto_lifts_on_contract_roll(tmp_path, monkeypatch):
    from types import SimpleNamespace

    sent = []
    monkeypatch.setattr("notifications.telegram.send_message",
                        lambda t, *a, **k: sent.append(t) or True)
    db = tmp_path / "roll.db"
    models.init_db(db)
    from scripts.pause_symbol import pause
    from scripts.run_bot import ContractBook, _contract_maintenance

    book = ContractBook(api=None)
    book.contracts = {"CRUDEOIL": {"symbol": "CRUDEOIL20JUL26FUT",
                                   "token": "1", "days_to_expiry": 0}}

    def fake_refresh():
        book.contracts = {"CRUDEOIL": {"symbol": "CRUDEOIL19AUG26FUT",
                                       "token": "2", "days_to_expiry": 30}}

    monkeypatch.setattr(book, "refresh", fake_refresh)
    pause("CRUDEOIL", "await AUG roll", db_path=db)
    assert models.get_state("symbol_pause:CRUDEOIL", "", db)

    engine = SimpleNamespace(db=db, monitor=None, expiry_days={})
    feed = SimpleNamespace(update_token=lambda s, t: None)
    _contract_maintenance(engine, feed, book)

    assert models.get_state("symbol_pause:CRUDEOIL", "", db) == ""
    with models._conn(db) as c:
        rows = c.execute("SELECT decision FROM decision_log "
                         "WHERE symbol='CRUDEOIL' ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["PAUSE", "RESUME"]
    assert any("RESUMED" in t for t in sent)


def test_scan_survives_one_symbol_failing(engine, monkeypatch):
    """A transient data failure on one instrument (Angel rate limit,
    2026-07-15) must not abort the scan for the others."""
    real = engine.feed.get_candles
    seen = []

    def flaky(symbol, interval="FIFTEEN_MINUTE", lookback=200):
        seen.append(symbol)
        if symbol == "CRUDEOIL":
            raise RuntimeError(
                "Couldn't parse the JSON response received from the "
                "server: b'Access denied because of exceeding access rate'")
        return real(symbol, interval, lookback)

    monkeypatch.setattr(engine.feed, "get_candles", flaky)
    engine._scan_for_entries(engine.feed.now)   # must not raise
    assert "GOLD" in seen                       # second symbol still scanned


def test_candle_fetch_backs_off_on_rate_limit(monkeypatch):
    from data import historical as h

    naps = []
    monkeypatch.setattr(h.time, "sleep", lambda s: naps.append(s))
    calls = {"n": 0}

    class _Api:
        def getCandleData(self, params):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("Access denied because of exceeding "
                                   "access rate")
            return {"data": [["2026-07-15T10:00:00+05:30",
                              1, 2, 0.5, 1.5, 10]]}

    df = h.fetch_ohlcv(_Api(), "12345", "FIFTEEN_MINUTE", days=2)
    assert len(df) == 1 and calls["n"] == 3     # two backoffs, then data
    assert any(n >= 5 for n in naps)            # real waits, not spins


def test_candle_fetch_raises_on_other_errors(monkeypatch):
    from data import historical as h
    monkeypatch.setattr(h.time, "sleep", lambda s: None)

    class _Api:
        def getCandleData(self, params):
            raise RuntimeError("Invalid Token")

    with pytest.raises(RuntimeError, match="Invalid Token"):
        h.fetch_ohlcv(_Api(), "12345", "FIFTEEN_MINUTE", days=2)


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
    # fully-intraday mode: the flat-at-end assertion below is only
    # guaranteed when no instrument may hold overnight (step 15 tests
    # cover the positional path separately)
    monkeypatch.setattr("config.settings.POSITIONAL_SYMBOLS", [])
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
