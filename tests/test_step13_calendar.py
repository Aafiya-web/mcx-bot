"""Step 13 tests: economic calendar feed — provider parsing, fallback,
daily cache, blackout window math, gate enforcement, engine wiring."""

from datetime import datetime, timedelta

import pytest

from config import settings
from core.regime import Regime
from data import economic_calendar as ec
from data.economic_calendar import (CalendarUnavailable, EconEvent,
                                    EconomicCalendar, FinnhubProvider,
                                    StaticWeeklyProvider, classify_event)
from database import models
from risk.gate_chain import run_gate_chain
from risk.portfolio_guard import PortfolioGuard
from risk.position_sizing import DailyLimitTracker
from strategies.base import Signal

WED = datetime(2026, 7, 8, 10, 0)  # a Wednesday, IST session hours

PAYLOAD = {"economicCalendar": [
    {"event": "FOMC Interest Rate Decision", "country": "US",
     "time": "2026-07-08 18:00:00", "impact": "high"},
    {"event": "EIA Crude Oil Stocks Change", "country": "US",
     "time": "2026-07-08 14:30:00", "impact": "medium"},
    {"event": "CPI (YoY)", "country": "DE",           # non-US CPI: dropped
     "time": "2026-07-08 06:00:00", "impact": "high"},
    {"event": "Ifo Business Climate", "country": "DE",  # irrelevant
     "time": "2026-07-08 08:00:00", "impact": "high"},
]}


class _Resp:
    def __init__(self, code=200, payload=None):
        self.status_code = code
        self._payload = payload
    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "cal.db"
    models.init_db(path)
    return path


# ---------------------------------------------------------- classification


def test_classify_event_map():
    assert classify_event("FOMC Interest Rate Decision", "US") == \
        ["CRUDEOIL", "NATURALGAS", "GOLD", "SILVER", "COPPER"]
    assert classify_event("EIA Crude Oil Stocks Change", "US") == ["CRUDEOIL"]
    assert classify_event("Natural Gas Storage", "US") == ["NATURALGAS"]
    assert classify_event("OPEC Meeting", "") == ["CRUDEOIL", "NATURALGAS"]
    assert classify_event("CPI (YoY)", "DE") == []      # US-only pattern
    assert classify_event("Ifo Business Climate", "DE") == []


# --------------------------------------------------------------- provider


def test_finnhub_parses_and_filters(monkeypatch):
    monkeypatch.setattr(settings, "FINNHUB_API_KEY", "k")
    monkeypatch.setattr(FinnhubProvider._session, "get",
                        lambda *a, **kw: _Resp(200, PAYLOAD))
    events = FinnhubProvider().fetch(WED.date(), WED.date())
    assert len(events) == 2                       # DE rows dropped
    fomc = next(e for e in events if "FOMC" in e.name)
    assert fomc.ts_utc == datetime(2026, 7, 8, 18, 0)
    assert fomc.ts_ist() == datetime(2026, 7, 8, 23, 30)
    assert set(fomc.symbols) == {"CRUDEOIL", "NATURALGAS", "GOLD",
                                 "SILVER", "COPPER"}
    assert fomc.source == "finnhub"


def test_finnhub_unavailable_without_key(monkeypatch):
    monkeypatch.setattr(settings, "FINNHUB_API_KEY", "")
    with pytest.raises(CalendarUnavailable, match="FINNHUB_API_KEY"):
        FinnhubProvider().fetch(WED.date(), WED.date())


def test_finnhub_premium_403_raises(monkeypatch):
    monkeypatch.setattr(settings, "FINNHUB_API_KEY", "k")
    monkeypatch.setattr(FinnhubProvider._session, "get",
                        lambda *a, **kw: _Resp(403, {}))
    with pytest.raises(CalendarUnavailable, match="403"):
        FinnhubProvider().fetch(WED.date(), WED.date())


def test_static_provider_generates_weekly_events():
    events = StaticWeeklyProvider().fetch(WED.date(),
                                          WED.date() + timedelta(days=1))
    names = {e.name for e in events}
    assert "EIA crude inventory" in names          # Wednesday
    assert "EIA natural gas storage" in names      # Thursday
    crude = next(e for e in events if "crude" in e.name)
    assert crude.ts_ist().time() == datetime(1, 1, 1, 20, 0).time()


# ---------------------------------------------------------------- service


def test_service_falls_back_and_logs(db, monkeypatch, caplog):
    monkeypatch.setattr(settings, "FINNHUB_API_KEY", "k")
    monkeypatch.setattr(FinnhubProvider._session, "get",
                        lambda *a, **kw: _Resp(403, {}))
    cal = EconomicCalendar(db_path=db)
    with caplog.at_level("WARNING"):
        events = cal.get_events(WED)
    assert cal.source == "static-weekly"
    assert any("fallback" in r.message for r in caplog.records)
    assert any("EIA crude inventory" == e.name for e in events)


class _CountingProvider(StaticWeeklyProvider):
    name = "counting"
    calls = 0
    def fetch(self, from_date, to_date):
        _CountingProvider.calls += 1
        return super().fetch(from_date, to_date)


def test_daily_cache_one_fetch_per_day(db):
    _CountingProvider.calls = 0
    cal = EconomicCalendar(provider=_CountingProvider(), db_path=db)
    cal.get_events(WED)
    cal.get_events(WED + timedelta(hours=5))
    assert _CountingProvider.calls == 1
    # cache survives a fresh service instance (bot_state persistence)
    cal2 = EconomicCalendar(provider=_CountingProvider(), db_path=db)
    events = cal2.get_events(WED + timedelta(hours=8))
    assert _CountingProvider.calls == 1
    assert cal2.source == "counting"
    assert events
    # new day -> refetch
    cal2.get_events(WED + timedelta(days=1))
    assert _CountingProvider.calls == 2


def test_upcoming_window_math(db):
    now = WED.replace(hour=17, minute=0)          # 17:00 IST = 11:30 UTC
    now_utc = now - ec.IST_OFFSET

    class _Stub(StaticWeeklyProvider):
        name = "stub"
        def fetch(self, from_date, to_date):
            return [
                EconEvent("in-1h", now_utc + timedelta(hours=1), "US",
                          "high", ["CRUDEOIL"], self.name),
                EconEvent("in-4h", now_utc + timedelta(hours=4), "US",
                          "high", ["CRUDEOIL"], self.name),
                EconEvent("1h-ago", now_utc - timedelta(hours=1), "US",
                          "high", ["CRUDEOIL"], self.name),
                EconEvent("gold-only", now_utc + timedelta(hours=1), "US",
                          "high", ["GOLD"], self.name),
            ]

    cal = EconomicCalendar(provider=_Stub(), db_path=db)
    hits = cal.upcoming_for("CRUDEOIL", now, window_minutes=120)
    assert [e.name for e in hits] == ["in-1h"]
    assert cal.upcoming_for("SILVER", now, window_minutes=120) == []


# --------------------------------------------------------------- the gate


def _gate(db, symbol, events):
    guard = PortfolioGuard(max_drawdown_pct=10.0, db_path=db)
    signal = Signal("BUY", "supertrend", 6000.0, 5995.0, 6010.0, 2.0, 3.3,
                    "t")
    regime = Regime("TRENDING", "BULLISH", 30, 25, 10, 5, 0.8, 2.5,
                    "Supertrend", True)
    return run_gate_chain(signal, symbol, regime, [], 1_000_000.0,
                          DailyLimitTracker(1_000_000, 2.0), guard,
                          1_000_000.0, upcoming_events=events)


def test_gate_blocks_inside_blackout(db):
    ev = EconEvent("FOMC Interest Rate Decision",
                   datetime(2026, 7, 8, 12, 0), "US", "high",
                   ["CRUDEOIL"], "finnhub")
    res = _gate(db, "CRUDEOIL", [ev])
    assert not res.approved
    assert any(name == "event_blackout" and not ok
               for name, ok, _ in res.checks)
    assert "FOMC" in res.rejection_reason


def test_gate_ignores_other_symbols_events(db):
    ev = EconEvent("EIA crude inventory", datetime(2026, 7, 8, 12, 0),
                   "US", "high", ["CRUDEOIL"], "finnhub")
    res = _gate(db, "GOLD", [ev])
    assert res.approved


def test_gate_blackout_maps_minis_to_base(db):
    ev = EconEvent("EIA crude inventory", datetime(2026, 7, 8, 12, 0),
                   "US", "high", ["CRUDEOIL"], "finnhub")
    res = _gate(db, "CRUDEOILM", [ev])
    assert not res.approved


# ------------------------------------------------------------ macro agent


def test_macro_agent_reads_context_events():
    from agents.analysts import MacroAnalyst
    from agents.base import DecisionContext
    ev = EconEvent("FOMC Interest Rate Decision",
                   datetime(2026, 7, 8, 12, 0), "US", "high",
                   ["CRUDEOIL"], "finnhub")
    ctx = DecisionContext(
        symbol="CRUDEOIL", signal=Signal.hold("x", "r"),
        regime=None, mtf={},
        events_today=["FOMC Interest Rate Decision"],
        upcoming_events=[ev], calendar_source="finnhub")
    out = MacroAnalyst().run(ctx)
    assert "IMMINENT" in out.rationale
    assert "finnhub" in out.rationale
    assert out.fields["risk_flags"] == ["FOMC Interest Rate Decision"]


# ---------------------------------------------------------- engine wiring


def test_engine_blackout_rejection_end_to_end(tmp_path, monkeypatch):
    from broker.order_manager import PaperExecutor
    from core.engine import Engine
    from data.feed import MockFeed

    monkeypatch.setattr("notifications.telegram.send_message",
                        lambda *a, **k: True)
    db = tmp_path / "eng.db"
    feed = MockFeed(symbols=["CRUDEOIL"], n_bars=400, seed=42)
    now = feed.now.replace(hour=15, minute=0)
    now_utc = now - ec.IST_OFFSET

    class _Stub(StaticWeeklyProvider):
        name = "stub"
        def fetch(self, from_date, to_date):
            return [EconEvent("EIA crude inventory",
                              now_utc + timedelta(minutes=30), "US",
                              "high", ["CRUDEOIL"], self.name)]

    engine = Engine(feed, PaperExecutor(feed.get_ltp), db_path=db,
                    capital=1_000_000.0, symbols=["CRUDEOIL"],
                    calendar=EconomicCalendar(provider=_Stub(), db_path=db))

    # Force a candidate: permissive regime + always-BUY strategy.
    trending = Regime("TRENDING", "BULLISH", 30, 25, 10, 5, 0.8, 2.5,
                      "Supertrend", True)
    monkeypatch.setattr("core.engine.classify_regime", lambda df: trending)
    monkeypatch.setattr("core.engine.mtf_regime",
                        lambda feed, sym: {"consensus": True,
                                           "direction": "BULLISH",
                                           "confidence": "2/2"})

    class _AlwaysBuy:
        def generate(self, df15, df1h, regime, when):
            return Signal("BUY", "supertrend", 6000.0, 5995.0, 6010.0,
                          2.0, 3.3, "forced")
    engine.strategies["CRUDEOIL"] = _AlwaysBuy()

    engine.tick(now)
    assert engine.stats["candidates"] == 1
    assert engine.stats["approved"] == 0          # blackout blocked it
    with models._conn(db) as c:
        row = c.execute("SELECT rationale FROM decision_log WHERE "
                        "stage='risk_team'").fetchone()
    assert row is not None
    assert "BLOCKED" in row["rationale"]
    assert "EIA crude inventory" in row["rationale"]
