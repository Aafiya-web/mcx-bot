"""Step 15 — positional/intraday hybrid: overnight holds must be EARNED.

The default is always to square off; holding is the exception gated on
instrument, profit cushion, 1H trend, calendar, and expiry — and any
error during the eligibility check closes the position (fail-safe).
"""

from datetime import datetime

import pandas as pd
import pytest

from broker.order_manager import PaperExecutor
from config import settings
from core.engine import Engine
from data.feed import MockFeed
from database import models
from strategies.base import Signal

SQUAREOFF_TICK = datetime(2026, 7, 16, 23, 20)


def _trending_1h(direction: str = "up", bars: int = 120,
                 base: float = 140_000.0) -> pd.DataFrame:
    """Synthetic strongly-trending hourly candles (ADX ~100)."""
    step = 80.0 if direction == "up" else -80.0
    idx = pd.date_range("2026-07-01 09:00", periods=bars, freq="1h")
    close = pd.Series([base + step * i for i in range(bars)], index=idx)
    return pd.DataFrame({"open": close - step / 2, "high": close + 40,
                         "low": close - 40, "close": close,
                         "volume": 500.0}, index=idx)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("notifications.telegram.send_message",
                        lambda text, *a, **k: sent.append(text) or True)
    monkeypatch.setattr(settings, "POSITIONAL_SYMBOLS", ["GOLD", "COPPER"])
    monkeypatch.setattr(settings, "OVERNIGHT_MIN_R", 0.5)
    db = tmp_path / "pos.db"
    feed = MockFeed(symbols=["CRUDEOIL", "GOLD"], n_bars=400, seed=7)
    eng = Engine(feed, PaperExecutor(feed.get_ltp), db_path=db,
                 capital=1_000_000.0, symbols=["CRUDEOIL", "GOLD"])
    eng._telegram_log = sent
    return eng


def _open_gold(engine, entry=141_000.0, stop=140_000.0, target=143_000.0):
    """GOLD long: r_unit = (143000-141000)/2 = 1000."""
    sig = Signal("BUY", "ema_trend", entry, stop, target, 2.0, 500.0, "t")
    engine.feed.set_ltp("GOLD", entry)
    return engine.monitor.open_position("GOLD", sig, 1)


def test_hold_when_trend_and_cushion(engine, monkeypatch):
    _open_gold(engine)
    engine.feed.set_ltp("GOLD", 141_700.0)          # +0.7R cushion
    monkeypatch.setattr(engine.feed, "get_candles",
                        lambda s, i="ONE_HOUR", lb=210: _trending_1h("up"))
    engine.tick(SQUAREOFF_TICK)
    open_pos = models.get_open_positions(engine.db)
    assert len(open_pos) == 1 and open_pos[0]["symbol"] == "GOLD"
    assert any("HOLDING OVERNIGHT" in t for t in engine._telegram_log)


def test_no_hold_without_profit_cushion(engine, monkeypatch):
    _open_gold(engine)
    engine.feed.set_ltp("GOLD", 141_200.0)          # +0.2R < 0.5R
    monkeypatch.setattr(engine.feed, "get_candles",
                        lambda s, i="ONE_HOUR", lb=210: _trending_1h("up"))
    engine.tick(SQUAREOFF_TICK)
    assert models.get_open_positions(engine.db) == []
    row = models.get_daily_pnl(db_path=engine.db)["trades"][-1]
    assert row["exit_reason"] == "SQUAREOFF"


def test_no_hold_when_trend_died(engine, monkeypatch):
    _open_gold(engine)
    engine.feed.set_ltp("GOLD", 141_700.0)          # cushion fine
    monkeypatch.setattr(engine.feed, "get_candles",   # ...but 1H bearish
                        lambda s, i="ONE_HOUR", lb=210: _trending_1h("down"))
    engine.tick(SQUAREOFF_TICK)
    assert models.get_open_positions(engine.db) == []


def test_intraday_instrument_always_squares_off(engine, monkeypatch):
    sig = Signal("BUY", "supertrend", 6000.0, 5900.0, 6200.0, 2.0, 60.0, "t")
    engine.feed.set_ltp("CRUDEOIL", 6000.0)
    engine.monitor.open_position("CRUDEOIL", sig, 1)
    engine.feed.set_ltp("CRUDEOIL", 6150.0)         # deep in profit
    monkeypatch.setattr(engine.feed, "get_candles",  # and trending
                        lambda s, i="ONE_HOUR", lb=210: _trending_1h("up",
                                                                     base=6e3))
    engine.tick(SQUAREOFF_TICK)
    assert models.get_open_positions(engine.db) == []   # not positional


def test_eligibility_error_fails_toward_closing(engine, monkeypatch):
    _open_gold(engine)
    engine.feed.set_ltp("GOLD", 141_700.0)

    def boom(*a, **k):
        raise RuntimeError("feed died")

    monkeypatch.setattr(engine.feed, "get_candles", boom)
    engine.tick(SQUAREOFF_TICK)
    assert models.get_open_positions(engine.db) == []


def test_no_hold_near_expiry(engine, monkeypatch):
    _open_gold(engine)
    engine.feed.set_ltp("GOLD", 141_700.0)
    monkeypatch.setattr(engine.feed, "get_candles",
                        lambda s, i="ONE_HOUR", lb=210: _trending_1h("up"))
    engine.expiry_days = {"GOLD": 3}
    engine.tick(SQUAREOFF_TICK)
    assert models.get_open_positions(engine.db) == []


def test_no_hold_with_overnight_event(engine, monkeypatch):
    from data.economic_calendar import EconEvent
    _open_gold(engine)
    engine.feed.set_ltp("GOLD", 141_700.0)
    monkeypatch.setattr(engine.feed, "get_candles",
                        lambda s, i="ONE_HOUR", lb=210: _trending_1h("up"))
    ev = EconEvent("FOMC Interest Rate Decision",
                   datetime(2026, 7, 17, 0, 30), "US", "high",
                   ["GOLD"], "test")
    monkeypatch.setattr(engine.calendar, "upcoming_for",
                        lambda base, now, window_minutes=None: [ev])
    engine.tick(SQUAREOFF_TICK)
    assert models.get_open_positions(engine.db) == []


def test_morning_gap_through_stop_exits_and_audits(engine, monkeypatch):
    """A held position that gaps through its stop overnight must exit at
    the (worse) opening price on the first tick — with slippage recorded,
    not forgiven."""
    _open_gold(engine)                               # stop at 140,000
    engine.feed.set_ltp("GOLD", 139_400.0)           # gapped 600 below stop
    events = engine.monitor.check()
    assert len(events) == 1
    ev = events[0]
    assert ev.exit_reason == "STOP_LOSS"
    assert ev.fill_price < ev.intended_price         # gap slippage visible
    assert models.get_open_positions(engine.db) == []


def test_live_product_type_by_instrument(monkeypatch):
    from broker.order_manager import LiveExecutor
    monkeypatch.setattr(settings, "LIVE_TRADING", True)
    monkeypatch.setattr(settings, "INITIAL_CAPITAL", 500_000.0)
    monkeypatch.setattr(settings, "MAX_DAILY_LOSS_PCT", 2.0)
    monkeypatch.setattr(settings, "MAX_DRAWDOWN_PCT", 10.0)
    monkeypatch.setattr(settings, "ANGEL_API_KEY", "k")
    monkeypatch.setattr(settings, "ANGEL_CLIENT_ID", "c")
    monkeypatch.setattr(settings, "ANGEL_PASSWORD", "p")
    monkeypatch.setattr(settings, "ANGEL_TOTP_KEY", "t")
    monkeypatch.setattr(settings, "ALGO_ID_TAG", "TAG1")

    ex = LiveExecutor(
        contract_fn=lambda s: (s + "16JUL26FUT", "42"),
        product_fn=lambda s: "CARRYFORWARD" if s == "GOLD" else "INTRADAY")
    p_gold = ex._build_params("GOLD", "BUY", 1, "MARKET", "NORMAL")
    p_crude = ex._build_params("CRUDEOIL", "BUY", 1, "MARKET", "NORMAL")
    assert p_gold["producttype"] == "CARRYFORWARD"
    assert p_crude["producttype"] == "INTRADAY"


def test_backtest_hold_mirror(monkeypatch):
    from backtest.engine import _bt_may_hold
    monkeypatch.setattr(settings, "POSITIONAL_SYMBOLS", ["GOLD"])
    monkeypatch.setattr(settings, "OVERNIGHT_MIN_R", 0.5)

    win = _trending_1h("up", bars=60)
    # resampling an hourly frame is a no-op; reuse as the 15-min window
    pos = {"side": "BUY", "entry": 140_000.0, "tp": 142_000.0}

    assert _bt_may_hold("GOLD", pos, 141_000.0, win)        # +1R, trending
    assert not _bt_may_hold("GOLD", pos, 140_200.0, win)    # thin cushion
    assert not _bt_may_hold("CRUDEOIL", pos, 141_000.0, win)  # intraday
    down = _trending_1h("down", bars=60)
    assert not _bt_may_hold("GOLD", pos, 141_000.0, down)   # wrong way


def test_backstop_refresh_replaces_orders(engine):
    _open_gold(engine)
    om = engine.monitor.om
    old_pending = set(om.pending)
    n = engine.monitor.refresh_backstops()
    assert n == 1
    assert set(om.pending) != old_pending        # new order id resting
    assert len(om.pending) == 1                  # old one cancelled