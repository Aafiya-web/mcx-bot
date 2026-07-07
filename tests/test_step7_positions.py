"""Step 7 tests: stop fires on time at the stored price, trailing only
tightens, backstops stay in sync, rollover reopens exposure."""

from datetime import date

import pytest

from broker.order_manager import PaperExecutor
from config import settings
from database import models
from positions import rollover as ro
from positions.monitor import PositionMonitor
from strategies.base import Signal


class StubFeed:
    def __init__(self, prices):
        self.prices = dict(prices)
    def get_ltp(self, symbol):
        return self.prices[symbol]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PAPER_SLIPPAGE_PCT", 0.0)  # exact math
    db = tmp_path / "pos.db"
    models.init_db(db)
    feed = StubFeed({"CRUDEOIL": 6000.0})
    om = PaperExecutor(feed.get_ltp)
    return feed, om, PositionMonitor(feed, om, db_path=db), db


def _buy(monitor, entry=6000.0, sl=5950.0, tp=6100.0):
    sig = Signal("BUY", "supertrend", entry, sl, tp, 2.0, 33.0, "test")
    return monitor.open_position("CRUDEOIL", sig, 2)


def test_entry_places_backstop(env):
    feed, om, monitor, db = env
    tid = _buy(monitor)
    assert len(models.get_open_positions(db)) == 1
    assert len(om.pending) == 1  # resting SL-M at the exchange
    resting = next(iter(om.pending.values()))
    assert resting.side == "SELL" and resting.price == 5950.0
    assert monitor._backstops[tid] == resting.order_id


def test_stop_fires_at_stored_price(env):
    feed, om, monitor, db = env
    tid = _buy(monitor)
    feed.prices["CRUDEOIL"] = 5940.0  # gap through the stop
    events = monitor.check()
    assert len(events) == 1
    ev = events[0]
    assert ev.exit_reason == "STOP_LOSS"
    assert ev.intended_price == 5950.0
    assert ev.fill_price == 5940.0            # market fill at LTP
    assert ev.slippage_pct == pytest.approx(10 / 5950 * 100)
    assert ev.pnl == pytest.approx((5940 - 6000) * 2 * 100)
    assert models.get_open_positions(db) == []
    assert om.pending == {}                   # backstop cancelled


def test_target_closes_position(env):
    feed, om, monitor, db = env
    _buy(monitor)
    feed.prices["CRUDEOIL"] = 6105.0
    events = monitor.check()
    assert events[0].exit_reason == "TAKE_PROFIT"
    assert events[0].pnl == pytest.approx((6105 - 6000) * 2 * 100)


def test_breakeven_at_1r(env):
    feed, om, monitor, db = env
    tid = _buy(monitor)                        # R = 50 points
    feed.prices["CRUDEOIL"] = 6055.0           # +1.1R
    assert monitor.check() == []               # no exit
    pos = models.get_open_positions(db)[0]
    assert pos["stop_loss"] == pytest.approx(6000.0)  # breakeven
    # backstop replaced at the new trigger
    resting = next(iter(om.pending.values()))
    assert resting.price == pytest.approx(6000.0)


def test_trailing_at_1_5r_follows_peak(env):
    feed, om, monitor, db = env
    _buy(monitor, tp=6500.0)                   # far target, let it run
    feed.prices["CRUDEOIL"] = 6090.0           # +1.8R, peak 6090
    monitor.check()
    pos = models.get_open_positions(db)[0]
    assert pos["stop_loss"] == pytest.approx(6040.0)  # peak - 1R

    feed.prices["CRUDEOIL"] = 6120.0           # new peak
    monitor.check()
    pos = models.get_open_positions(db)[0]
    assert pos["stop_loss"] == pytest.approx(6070.0)

    feed.prices["CRUDEOIL"] = 6080.0           # pullback: stop must NOT move
    monitor.check()
    pos = models.get_open_positions(db)[0]
    assert pos["stop_loss"] == pytest.approx(6070.0)


def test_short_stop_fires_upward(env):
    feed, om, monitor, db = env
    sig = Signal("SELL", "supertrend", 6000.0, 6050.0, 5900.0, 2.0, 33.0, "t")
    monitor.open_position("CRUDEOIL", sig, 1)
    feed.prices["CRUDEOIL"] = 6060.0
    events = monitor.check()
    assert events[0].exit_reason == "STOP_LOSS"
    assert events[0].pnl == pytest.approx((6000 - 6060) * 1 * 100)


def test_close_all_squareoff(env):
    feed, om, monitor, db = env
    _buy(monitor)
    events = monitor.close_all("SQUAREOFF")
    assert len(events) == 1 and events[0].exit_reason == "SQUAREOFF"
    assert models.get_open_positions(db) == []


# ---------------------------------------------------------------- rollover


def test_parse_expiry():
    assert ro.parse_expiry("CRUDEOIL26JULFUT") == date(2026, 7, 20)
    assert ro.parse_expiry("SILVERM26DECFUT") == date(2026, 12, 20)
    assert ro.parse_expiry("NOT_A_FUTURE") is None


def test_days_to_expiry_and_needs_rollover():
    days = ro.days_to_expiry("CRUDEOIL26JULFUT", today=date(2026, 7, 16))
    assert days == 4
    assert ro.needs_rollover("CRUDEOIL", 4) is False   # roll_days = 3
    assert ro.needs_rollover("CRUDEOIL", 3) is True
    assert ro.needs_rollover("GOLD", 5) is True        # roll_days = 5


def test_expiry_alert_ladder():
    alerts = ro.expiry_alerts({"CRUDEOIL": 0, "GOLD": 2, "SILVER": 6,
                               "COPPER": 15})
    text = "\n".join(alerts)
    assert "TODAY" in text and "2d" in text and "6d" in text
    assert "COPPER" not in text


def test_rollover_reopens_same_exposure(env, monkeypatch):
    monkeypatch.setattr("notifications.telegram.send_message",
                        lambda *a, **k: True)
    feed, om, monitor, db = env
    tid = _buy(monitor)
    pos = models.get_open_positions(db)[0]

    # Positions stay keyed by the BASE symbol; the contract month changes
    # only at the execution boundary, via switch_fn between close & reopen.
    events = []
    new_id = ro.rollover_position(
        monitor, "CRUDEOIL", pos,
        {"symbol": "CRUDEOIL26AUGFUT", "token": "99", "days_to_expiry": 40},
        switch_fn=lambda: events.append("switched"))
    assert new_id is not None and new_id != tid
    assert events == ["switched"]

    open_pos = models.get_open_positions(db)
    assert len(open_pos) == 1
    assert open_pos[0]["symbol"] == "CRUDEOIL"   # base name, not the FUT
    assert open_pos[0]["side"] == "BUY"
    assert open_pos[0]["qty"] == 2
    # the close order went out BEFORE the switch (old contract month)
    close_fill = [f for f in om.fills if f.tag == "ROLLOVER"][0]
    entry_fills = [f for f in om.fills if f.tag != "ROLLOVER"
                   and f.side == "BUY"]
    assert close_fill.side == "SELL"
    assert len(entry_fills) == 2                 # original entry + reopen
