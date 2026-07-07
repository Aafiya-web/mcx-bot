"""Step 14 tests: the landmine fixes (HANDOFF §3b L1/L2/L3/L4/L6/L7/L8)."""

from datetime import date, datetime

import pytest

from broker.order_manager import PaperExecutor
from config import settings
from core.engine import Engine
from data.feed import LiveFeed, MockFeed
from database import models
from positions import rollover as ro
from positions.monitor import PositionMonitor
from risk.portfolio_guard import PortfolioGuard
from risk.position_sizing import DailyLimitTracker
from strategies.base import Signal


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "hard.db"
    models.init_db(path)
    return path


# ------------------------------------------------- L1: scan-on-bar-close


def test_engine_scans_once_per_15min_bucket(tmp_path, monkeypatch):
    monkeypatch.setattr("notifications.telegram.send_message",
                        lambda *a, **k: True)
    feed = MockFeed(symbols=["CRUDEOIL"], n_bars=400, seed=42)
    engine = Engine(feed, PaperExecutor(feed.get_ltp),
                    db_path=tmp_path / "l1.db", symbols=["CRUDEOIL"])
    scans = {"n": 0}
    monkeypatch.setattr(engine, "_scan_for_entries",
                        lambda now: scans.__setitem__("n", scans["n"] + 1))

    engine.tick(datetime(2026, 7, 6, 10, 0))    # new bucket -> scan
    engine.tick(datetime(2026, 7, 6, 10, 3))    # same bucket -> skip
    engine.tick(datetime(2026, 7, 6, 10, 14))   # same bucket -> skip
    assert scans["n"] == 1
    engine.tick(datetime(2026, 7, 6, 10, 15))   # next bar -> scan
    assert scans["n"] == 2
    assert engine.stats["scans"] == 2
    # stop monitoring still ran on EVERY tick
    assert engine.stats["ticks"] == 4


# -------------------------------------------- L2: interval-aware lookback


def test_livefeed_fetch_depth_is_interval_aware(monkeypatch):
    import pandas as pd
    captured = {}

    def fake_fetch(api, token, interval, days, exchange="MCX"):
        captured[interval] = days
        return pd.DataFrame(columns=["open", "high", "low", "close",
                                     "volume"])

    monkeypatch.setattr("broker.auto_login.get_api", lambda: object())
    monkeypatch.setattr("data.historical.fetch_ohlcv", fake_fetch)
    feed = LiveFeed({"GOLD": "1"})

    feed.get_candles("GOLD", "ONE_HOUR", lookback=210)
    # 210 hourly bars at ~14.5/day needs ~15 trading days minimum
    assert captured["ONE_HOUR"] >= 15

    feed.get_candles("GOLD", "FIFTEEN_MINUTE", lookback=200)
    assert 4 <= captured["FIFTEEN_MINUTE"] <= 10   # ~3.5 days + margin


# ----------------------------------------- L3: hourly backtest lookback


def test_lookback_for_timeframes():
    from backtest.engine import LOOKBACK, LOOKBACK_HOURLY, lookback_for
    assert lookback_for("ONE_HOUR") == LOOKBACK_HOURLY
    assert lookback_for("FIFTEEN_MINUTE") == LOOKBACK


def test_hourly_window_clears_ema_warmup():
    from data.feed import _synth_ohlcv
    from strategies.ema_trend import EmaTrendStrategy
    df = _synth_ohlcv(72000.0, 1000, seed=9)
    window = df.tail(901)
    h1 = (window.resample("1h")
          .agg({"open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum"}).dropna())
    assert len(h1) >= 202                       # EMA-200 warmup satisfied
    from core.regime import Regime
    trending = Regime("TRENDING", "BULLISH", 30, 25, 10, 5, 0.8, 2.5,
                      "x", True)
    sig = EmaTrendStrategy().generate(window, h1, trending,
                                      datetime(2026, 7, 6, 15, 0))
    assert "warmup" not in sig.reason           # it can actually evaluate


# --------------------------------- L4: backstop beat-the-monitor race


def test_backstop_fill_is_reconciled_not_doubled(db, monkeypatch):
    monkeypatch.setattr(settings, "PAPER_SLIPPAGE_PCT", 0.0)

    class StubFeed:
        def __init__(self):
            self.price = 6000.0
        def get_ltp(self, symbol):
            return self.price

    feed = StubFeed()
    om = PaperExecutor(feed.get_ltp)
    monitor = PositionMonitor(feed, om, db_path=db)
    sig = Signal("BUY", "supertrend", 6000.0, 5950.0, 6100.0, 2.0, 33.0,
                 "t")
    tid = monitor.open_position("CRUDEOIL", sig, 2)

    feed.price = 5940.0            # breach
    om.process_pending()           # the 'exchange' backstop fills FIRST
    fills_before = len(om.fills)   # entry + backstop = 2

    events = monitor.check()       # monitor arrives second
    assert len(events) == 1
    ev = events[0]
    assert ev.exit_reason == "STOP_LOSS"
    assert ev.intended_price == 5950.0
    assert ev.fill_price == pytest.approx(5940.0)   # the backstop's fill
    assert len(om.fills) == fills_before            # NO extra market order
    assert models.get_open_positions(db) == []      # closed exactly once
    assert ev.pnl == pytest.approx((5940 - 6000) * 2 * 100)


def test_normal_close_still_cancels_backstop_first(db, monkeypatch):
    monkeypatch.setattr(settings, "PAPER_SLIPPAGE_PCT", 0.0)

    class StubFeed:
        def __init__(self):
            self.price = 6000.0
        def get_ltp(self, symbol):
            return self.price

    feed = StubFeed()
    om = PaperExecutor(feed.get_ltp)
    monitor = PositionMonitor(feed, om, db_path=db)
    sig = Signal("BUY", "supertrend", 6000.0, 5950.0, 6100.0, 2.0, 33.0,
                 "t")
    monitor.open_position("CRUDEOIL", sig, 1)
    feed.price = 6105.0
    events = monitor.check()
    assert events[0].exit_reason == "TAKE_PROFIT"
    assert om.pending == {}        # backstop cancelled, nothing resting


# ------------------------------------- L6: daily limits persist + reset


def test_daily_tracker_survives_restart(db):
    t1 = DailyLimitTracker(1_000_000, 2.0, persist=True, db_path=db)
    t1.roll_date(date(2026, 7, 6))
    t1.record_entry()
    t1.record_close(-15_000.0)
    t1.record_close(-5_000.0)

    # "restart": a fresh instance loads the same counters
    t2 = DailyLimitTracker(1_000_000, 2.0, persist=True, db_path=db)
    assert t2.daily_pnl == pytest.approx(-20_000.0)
    assert t2.trades_today == 1
    assert t2.consecutive_losses == 2
    # exactly at the 2% limit (₹20k of ₹10L): blocked — the tracker uses
    # <= deliberately so the boundary case fails safe
    assert t2.can_trade()[0] is False


def test_daily_tracker_resets_on_new_day(db):
    t = DailyLimitTracker(1_000_000, 2.0, persist=True, db_path=db)
    t.roll_date(date(2026, 7, 6))
    t.record_close(-50_000.0)
    assert t.can_trade()[0] is False
    t.roll_date(date(2026, 7, 7))               # next session
    assert t.daily_pnl == 0.0
    assert t.can_trade()[0] is True
    # and the reset persisted
    t2 = DailyLimitTracker(1_000_000, 2.0, persist=True, db_path=db)
    assert t2.daily_pnl == 0.0 and t2.date == "2026-07-07"


def test_unpersisted_tracker_untouched_by_db(db):
    t = DailyLimitTracker(1_000_000, 2.0)       # tests/ad-hoc usage
    t.record_close(-1000.0)
    assert models.get_state("daily_tracker", "", db) == ""


# --------------------------------------- L7: equity peak persistence


def test_guard_peak_survives_restart(db):
    g1 = PortfolioGuard(max_drawdown_pct=10.0, db_path=db)
    g1.update(1_200_000.0)
    # "restart"
    g2 = PortfolioGuard(max_drawdown_pct=10.0, db_path=db)
    assert g2.equity_peak == pytest.approx(1_200_000.0)
    assert g2.update(1_150_000.0) is False      # -4.2%: fine
    assert g2.update(1_070_000.0) is True       # -10.8% from the OLD peak
    assert models.is_halted(db)


def test_manual_reset_clears_persisted_peak(db, monkeypatch):
    monkeypatch.setattr("notifications.telegram.send_message",
                        lambda *a, **k: True)
    g = PortfolioGuard(max_drawdown_pct=10.0, db_path=db)
    g.update(1_000_000.0)
    g.update(850_000.0)                          # trip
    g.manual_reset()
    g3 = PortfolioGuard(max_drawdown_pct=10.0, db_path=db)
    assert g3.equity_peak is None                # re-arms from here


# ------------------------------------------- L8: expiry + rollover wiring


def test_engine_expiry_fn_reaches_risk_team(tmp_path, monkeypatch):
    monkeypatch.setattr("notifications.telegram.send_message",
                        lambda *a, **k: True)
    db = tmp_path / "l8.db"
    feed = MockFeed(symbols=["CRUDEOIL"], n_bars=400, seed=42)
    engine = Engine(feed, PaperExecutor(feed.get_ltp), db_path=db,
                    capital=1_000_000.0, symbols=["CRUDEOIL"],
                    expiry_fn=lambda: {"CRUDEOIL": 1})   # expiry tomorrow

    from core.regime import Regime
    trending = Regime("TRENDING", "BULLISH", 30, 25, 10, 5, 0.8, 2.5,
                      "x", True)
    monkeypatch.setattr("core.engine.classify_regime", lambda d: trending)
    monkeypatch.setattr("core.engine.mtf_regime",
                        lambda f, s: {"consensus": True,
                                      "direction": "BULLISH",
                                      "confidence": "2/2"})

    class _AlwaysBuy:
        def generate(self, df15, df1h, regime, when):
            return Signal("BUY", "supertrend", 6000.0, 5995.0, 6010.0,
                          2.0, 3.3, "forced")
    engine.strategies["CRUDEOIL"] = _AlwaysBuy()

    engine.tick(datetime(2026, 7, 6, 15, 0))
    assert engine.stats["candidates"] == 1
    assert engine.stats["approved"] == 0         # conservative expiry veto
    with models._conn(db) as c:
        row = c.execute("SELECT rationale FROM decision_log WHERE "
                        "stage='risk_team'").fetchone()
    assert "expiry" in row["rationale"]


def test_get_next_contract_picks_second_nearest():
    class StubApi:
        def searchScrip(self, exchange, base):
            return {"data": [
                {"tradingsymbol": "CRUDEOIL26SEPFUT", "symboltoken": "3"},
                {"tradingsymbol": "CRUDEOIL26JULFUT", "symboltoken": "1"},
                {"tradingsymbol": "CRUDEOIL26AUGFUT", "symboltoken": "2"},
                {"tradingsymbol": "CRUDEOIL25JANFUT", "symboltoken": "0"},
            ]}
    nxt = ro.get_next_contract(StubApi(), "CRUDEOIL",
                               today=date(2026, 7, 7))
    assert nxt["symbol"] == "CRUDEOIL26AUGFUT"
    assert nxt["token"] == "2"
