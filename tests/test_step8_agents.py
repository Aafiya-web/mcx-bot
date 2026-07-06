"""Step 8 tests: full agent pipeline on a clean candidate, veto paths,
LLM disabled by default, bounded adaptation with changelog."""

import pytest

from agents.base import DecisionContext, ask_llm
from agents.reflection import PARAM_BOUNDS, apply_param
from core.orchestrator import Orchestrator
from core.regime import Regime
from database import models
from risk.portfolio_guard import PortfolioGuard
from risk.position_sizing import DailyLimitTracker
from strategies.base import Signal

TRENDING = Regime("STRONG_TREND", "BULLISH", 34, 28, 12, 5, 0.8, 3.1,
                  "Supertrend", True)
RANGING = Regime("RANGING", "NEUTRAL", 15, 12, 11, 5, 0.5, 1.0, "HOLD", False)


def _ctx(**over):
    base = dict(
        symbol="CRUDEOIL",
        signal=Signal("BUY", "supertrend", 6000.0, 5995.0, 6010.0, 2.0,
                      3.3, "flip, 1H agrees"),
        regime=TRENDING,
        mtf={"consensus": True, "direction": "BULLISH",
             "confidence": "2/2 timeframes tradeable"},
        volume_ratio=1.8,
        open_positions=[],
        capital=1_000_000.0,
        equity=1_000_000.0,
        days_to_expiry=20,
        weekday=0,  # Monday: no scheduled events
    )
    base.update(over)
    return DecisionContext(**base)


@pytest.fixture
def orch(tmp_path):
    db = tmp_path / "orch.db"
    models.init_db(db)
    guard = PortfolioGuard(max_drawdown_pct=10.0, db_path=db)
    daily = DailyLimitTracker(1_000_000, 2.0)
    return Orchestrator(daily, guard, db_path=db), db


def test_clean_candidate_approved_and_logged(orch):
    o, db = orch
    d = o.evaluate(_ctx())
    assert d.approved and d.action == "BUY" and d.lots >= 1
    assert {"technical", "macro", "regime", "bull", "bear", "trader",
            "risk_team", "pm"} <= set(d.stages)
    # every stage persisted to the decision log
    with models._conn(db) as c:
        stages = {r["stage"] for r in
                  c.execute("SELECT stage FROM decision_log")}
    assert "pm" in stages and "trader" in stages
    assert len(stages) == 8


def test_ranging_regime_rejected_by_pipeline(orch):
    o, _ = orch
    d = o.evaluate(_ctx(regime=RANGING,
                        mtf={"consensus": False, "direction": "NEUTRAL",
                             "confidence": "0/2"}))
    assert not d.approved and d.lots == 0
    assert "veto" in d.rationale.lower() or "REJECT" in d.stages["pm"].stance


def test_close_debate_yields_no_proposal(orch):
    o, _ = orch
    # Weak everything: neutral regime direction, no consensus, thin volume,
    # expiry near -> bear catches up, trader stands down.
    d = o.evaluate(_ctx(
        regime=Regime("TRENDING", "BEARISH", 26, 10, 22, 5, 0.8, 1.8,
                      "Supertrend", True),
        mtf={"consensus": False, "direction": "BEARISH",
             "confidence": "1/2"},
        volume_ratio=0.8,
        days_to_expiry=3,
        consecutive_losses=2,
    ))
    assert not d.approved
    assert d.stages["trader"].fields["proposed"] is False


def test_expiry_tomorrow_conservative_veto(orch):
    o, _ = orch
    d = o.evaluate(_ctx(days_to_expiry=1))
    assert not d.approved
    assert "expiry" in d.stages["risk_team"].rationale


def test_event_day_halves_size(orch):
    o, _ = orch
    normal = o.evaluate(_ctx()).lots
    event_day = o.evaluate(_ctx(weekday=2)).lots  # Wednesday: EIA crude
    assert event_day == max(1, normal // 2)


def test_correlated_position_blocks_via_gate(orch):
    o, _ = orch
    d = o.evaluate(_ctx(open_positions=[
        {"symbol": "NATURALGAS", "side": "BUY", "qty": 1,
         "entry_price": 250.0}]))
    assert not d.approved
    assert "correlated" in d.stages["risk_team"].rationale


def test_llm_disabled_returns_none():
    assert ask_llm("anything") is None  # no key configured in tests


# -------------------------------------------------------------- adaptation


def test_apply_param_clamps_and_logs(tmp_path):
    db = tmp_path / "adapt.db"
    models.init_db(db)
    import core.regime as regime_mod
    original = regime_mod.ADX_TRENDING
    try:
        changed, applied = apply_param("ADX_TRENDING", 99.0,
                                       "test overshoot", db)
        assert changed
        assert applied == PARAM_BOUNDS["ADX_TRENDING"]["hi"]  # clamped
        assert regime_mod.ADX_TRENDING == applied
        hist = models.get_param_history("ADX_TRENDING", db)
        assert len(hist) == 1
        assert hist[0]["old_value"] == original
        assert "clamped" in hist[0]["reason"]
    finally:
        regime_mod.ADX_TRENDING = original


def test_non_registered_param_refused(tmp_path):
    db = tmp_path / "adapt.db"
    models.init_db(db)
    changed, _ = apply_param("MAX_DRAWDOWN_PCT", 50.0,
                             "adaptation must never touch safety", db)
    assert changed is False
    assert models.get_param_history(db_path=db) == []


def test_reflection_no_change_on_thin_history(orch):
    o, db = orch
    out = o.reflect()
    assert out.fields["applied"] == []          # <10 trades: hands off
    assert models.get_param_history(db_path=db) == []
