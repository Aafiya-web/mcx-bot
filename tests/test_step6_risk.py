"""Step 6 tests: every veto path in sizing, correlation, guard, and the
full gate chain."""

import pytest

from config import settings
from core.regime import Regime
from database import models
from risk.correlation import cluster_exposure_ok, correlation_check
from risk.gate_chain import run_gate_chain
from risk.portfolio_guard import PortfolioGuard, validate_stop_change
from risk.position_sizing import DailyLimitTracker, position_size
from strategies.base import Signal

TRENDING = Regime("TRENDING", "BULLISH", 30, 25, 10, 5, 0.8, 2.5,
                  "Supertrend", True)
RANGING = Regime("RANGING", "NEUTRAL", 15, 12, 11, 5, 0.5, 1.0, "HOLD", False)


# ------------------------------------------------------------------ sizing


def test_constant_rupee_risk():
    # ₹10L capital, 0.1% risk = ₹1,000. CRUDEOIL point value 100:
    # 5-point stop -> ₹500/lot -> 2 lots.
    lots, _ = position_size(1_000_000, "CRUDEOIL", 6000.0, 5995.0, 0.1)
    assert lots == 2


def test_wide_stop_smaller_size():
    lots, _ = position_size(1_000_000, "CRUDEOIL", 6000.0, 5990.0, 0.1)
    assert lots == 1  # 10-point stop -> ₹1,000/lot -> exactly 1


def test_unaffordable_lot_refused_not_rounded_up():
    # 20-point stop -> ₹2,000/lot > ₹1,000 budget -> refuse, never round up.
    lots, reason = position_size(1_000_000, "CRUDEOIL", 6000.0, 5980.0, 0.1)
    assert lots == 0
    assert "refused" in reason


def test_margin_cap_limits_lots(monkeypatch):
    monkeypatch.setattr(settings, "MAX_POSITION_MARGIN_PCT", 30.0)
    # Huge risk budget wants 200 lots, but CRUDEOIL margin ~₹60k/lot and
    # the 30% cap on ₹10L allows ₹3L -> 5 lots.
    lots, reason = position_size(1_000_000, "CRUDEOIL", 6000.0, 5995.0, 10.0)
    assert lots == 5
    assert "margin" in reason


def test_margin_cap_refuses_oversized_contract(monkeypatch):
    monkeypatch.setattr(settings, "MAX_POSITION_MARGIN_PCT", 30.0)
    # GOLD margin ~8% of ₹72L notional = ₹5.76L/lot, way over 30% of ₹1L.
    lots, reason = position_size(100_000, "GOLD", 72000.0, 71600.0, 100.0)
    assert lots == 0
    assert "margin" in reason and "refused" in reason


def test_zero_stop_distance_rejected():
    lots, reason = position_size(100_000, "CRUDEOIL", 6000.0, 6000.0)
    assert lots == 0 and "invalid stop" in reason


def test_daily_limit_tracker():
    d = DailyLimitTracker(capital=100_000, max_loss_pct=2.0)
    assert d.can_trade()[0]
    d.record_close(-2000.0)  # exactly the 2% limit
    ok, reason = d.can_trade()
    assert not ok and "daily loss limit" in reason
    d.reset()
    assert d.can_trade()[0]


def test_overtrading_blocked(monkeypatch):
    monkeypatch.setattr(settings, "MAX_TRADES_PER_DAY", 2)
    d = DailyLimitTracker(capital=100_000, max_loss_pct=2.0)
    d.record_entry(), d.record_entry()
    ok, reason = d.can_trade()
    assert not ok and "trades/day" in reason


# ------------------------------------------------------------- correlation


def test_same_direction_same_cluster_blocked():
    open_pos = [{"symbol": "NATURALGAS", "side": "BUY"}]
    ok, reason = correlation_check("CRUDEOIL", "BUY", open_pos)
    assert not ok and "double" in reason


def test_opposite_direction_same_cluster_allowed():
    open_pos = [{"symbol": "NATURALGAS", "side": "BUY"}]
    ok, _ = correlation_check("CRUDEOIL", "SELL", open_pos)
    assert ok


def test_cross_cluster_allowed():
    open_pos = [{"symbol": "GOLD", "side": "BUY"}]
    ok, _ = correlation_check("CRUDEOIL", "BUY", open_pos)
    assert ok


def test_mini_contract_shares_base_cluster():
    open_pos = [{"symbol": "GOLDM", "side": "BUY"}]
    ok, reason = correlation_check("SILVER", "BUY", open_pos)
    assert not ok and "PRECIOUS" in reason


def test_cluster_margin_cap():
    # CRUDEOIL 1 lot ~₹60k margin + NATURALGAS 1 lot ~₹37.5k = ~₹97.5k,
    # over the 50% cap on ₹1L capital (₹50k).
    open_pos = [{"symbol": "CRUDEOIL", "side": "SELL", "qty": 1,
                 "entry_price": 6000.0}]
    ok, reason = cluster_exposure_ok("NATURALGAS", 1, 250.0, open_pos,
                                     capital=100_000, max_cluster_pct=50.0)
    assert not ok and "cap" in reason


# ------------------------------------------------------------------- guard


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "guard.db"
    models.init_db(path)
    return path


def test_breaker_trips_and_persists(db, monkeypatch):
    monkeypatch.setattr("notifications.telegram.send_message",
                        lambda *a, **k: True)
    g = PortfolioGuard(max_drawdown_pct=10.0, db_path=db)
    assert g.update(100_000) is False
    assert g.update(95_000) is False       # -5%: fine
    assert g.update(89_000) is True        # -11%: TRIP
    assert models.is_halted(db) is True
    # a fresh guard instance (restart) still sees the halt
    g2 = PortfolioGuard(max_drawdown_pct=10.0, db_path=db)
    assert g2.update(200_000) is True      # no auto-resume, ever


def test_manual_reset_only(db, monkeypatch):
    monkeypatch.setattr("notifications.telegram.send_message",
                        lambda *a, **k: True)
    g = PortfolioGuard(max_drawdown_pct=10.0, db_path=db)
    g.update(100_000)
    g.update(85_000)
    assert g.halted
    g.manual_reset()
    assert not g.halted
    assert g.update(85_000) is False  # peak reset too


def test_unarmed_guard_never_trips(db):
    g = PortfolioGuard(max_drawdown_pct=None, db_path=db)
    g.max_drawdown_pct = None  # explicit: placeholder unset
    assert g.update(100_000) is False
    assert g.update(1_000) is False  # -99% and still no trip: DISARMED
    assert "DISARMED" in g.status(1_000)


def test_close_all_flattens_both_sides(db):
    class FakeOM:
        calls = []
        def place_market_order(self, symbol, side, qty, tag=""):
            self.calls.append((symbol, side, qty, tag))
    om = FakeOM()
    g = PortfolioGuard(max_drawdown_pct=10.0, db_path=db)
    g.close_all(om, [{"symbol": "CRUDEOIL", "side": "BUY", "qty": 2},
                     {"symbol": "GOLD", "side": "SELL", "qty": 1}])
    assert ("CRUDEOIL", "SELL", 2, "CIRCUIT_BREAKER") in om.calls
    assert ("GOLD", "BUY", 1, "CIRCUIT_BREAKER") in om.calls


def test_stop_may_only_tighten():
    long_pos = {"side": "BUY", "stop_loss": 5950.0}
    ok, _ = validate_stop_change(long_pos, 5970.0)   # tighten: OK
    assert ok
    ok, reason = validate_stop_change(long_pos, 5900.0)  # loosen: NO
    assert not ok and "cannot move long stop lower" in reason

    short_pos = {"side": "SELL", "stop_loss": 6050.0}
    ok, _ = validate_stop_change(short_pos, 6020.0)  # tighten: OK
    assert ok
    ok, reason = validate_stop_change(short_pos, 6100.0)
    assert not ok and "cannot move short stop higher" in reason


# -------------------------------------------------------------- gate chain


def _signal(action="BUY", entry=6000.0, sl=5995.0):
    return Signal(action, "supertrend", entry, sl, entry + 10, 2.0, 3.3,
                  "test")


def _chain(db, signal=None, regime=TRENDING, open_positions=None,
           daily=None, equity=1_000_000.0):
    guard = PortfolioGuard(max_drawdown_pct=10.0, db_path=db)
    return run_gate_chain(
        signal or _signal(), "CRUDEOIL", regime,
        open_positions or [], 1_000_000.0,
        daily or DailyLimitTracker(1_000_000, 2.0), guard, equity)


def test_clean_signal_approved(db):
    res = _chain(db)
    # 1% of ₹10L = ₹10k budget / ₹500 per lot = 20, margin-capped to 5.
    assert res.approved and res.lots == 5
    assert all(ok for _, ok, _ in res.checks)


def test_hold_signal_rejected(db):
    res = _chain(db, signal=Signal.hold("x", "nothing"))
    assert not res.approved
    assert res.checks[0][0] == "signal"


def test_chain_rechecks_regime(db):
    res = _chain(db, regime=RANGING)
    assert not res.approved
    assert any(name == "regime" and not ok for name, ok, _ in res.checks)


def test_chain_blocks_on_daily_loss(db):
    d = DailyLimitTracker(1_000_000, 2.0)
    d.record_close(-50_000)
    res = _chain(db, daily=d)
    assert not res.approved and "daily loss" in res.rejection_reason


def test_chain_blocks_on_correlation(db):
    res = _chain(db, open_positions=[{"symbol": "NATURALGAS", "side": "BUY",
                                      "qty": 1, "entry_price": 250.0}])
    assert not res.approved and "correlated" in res.rejection_reason


def test_chain_blocks_when_halted(db, monkeypatch):
    models.set_halted(True, db)
    res = _chain(db)
    assert not res.approved
    assert any(name == "halt_flag" and not ok for name, ok, _ in res.checks)


def test_chain_trips_breaker_on_drawdown(db, monkeypatch):
    monkeypatch.setattr("notifications.telegram.send_message",
                        lambda *a, **k: True)
    guard = PortfolioGuard(max_drawdown_pct=10.0, db_path=db)
    guard.update(1_200_000.0)  # establish peak
    res = run_gate_chain(_signal(), "CRUDEOIL", TRENDING, [], 1_000_000.0,
                         DailyLimitTracker(1_000_000, 2.0), guard,
                         1_000_000.0)
    assert not res.approved  # 16.7% below peak -> breaker fired inside chain
    assert models.is_halted(db)
