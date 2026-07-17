"""Step 17 — live-vs-replay divergence guard (scripts/replay_check.py).

The guard exists because of the forming-bar incident: a healthy-looking
bot that was structurally blind for a week. These tests seed the candle
cache with a series the pipeline MUST signal on, and prove the guard
notices when the live decision log doesn't match.
"""

from datetime import date

import pandas as pd
import pytest

from database import models
from scripts import replay_check as rc

DAY = date(2026, 7, 15)   # a Wednesday — inside market hours logic
# Two context sessions before DAY so the replay's 60-bar warmup is
# cleared before the evaluated day begins (Mon 13th + Tue 14th).
SEED_DAYS = [date(2026, 7, 13), date(2026, 7, 14), DAY]


def _session_bars(day: date, n: int = 40) -> pd.DatetimeIndex:
    """n 15-min bars inside one session, starting 10:00 IST."""
    return pd.date_range(f"{day} 10:00", periods=n, freq="15min",
                         tz="Asia/Kolkata")


def _seed_breakout_series(db, days: list[date]) -> None:
    """Monotone uptrend: every bar a fresh Donchian high, ADX ~ 100,
    periodic volume spikes to pass the breakout volume confirm."""
    from data.store import save_candles

    frames = []
    px = 250.0
    for d in days:
        idx = _session_bars(d)
        closes, vols = [], []
        for i in range(len(idx)):
            px += 0.9
            closes.append(px)
            vols.append(400.0 if i % 4 == 0 else 100.0)
        close = pd.Series(closes, index=idx)
        frames.append(pd.DataFrame(
            {"open": close - 0.5, "high": close + 0.7,
             "low": close - 0.7, "close": close, "volume": vols},
            index=idx))
    df15 = pd.concat(frames)
    save_candles("NATURALGAS", "FIFTEEN_MINUTE", df15, db_path=db)
    h1 = (df15.resample("1h").agg({"open": "first", "high": "max",
                                   "low": "min", "close": "last",
                                   "volume": "sum"}).dropna())
    save_candles("NATURALGAS", "ONE_HOUR", h1, db_path=db)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "replay.db"
    models.init_db(p)
    return p


def _insert_pm_rows(db, day: date, n: int) -> None:
    with models._conn(db) as c:
        for _ in range(n):
            c.execute(
                "INSERT INTO decision_log (ts, trigger, symbol, stage, "
                "decision, rationale, raw_json) VALUES (?,?,?,?,?,?,?)",
                (f"{day} 11:00:00", "candidate_signal", "NATURALGAS",
                 "pm", "REJECT", "test", "{}"))


def test_replay_finds_signals_on_seeded_breakout(db):
    _seed_breakout_series(db, SEED_DAYS)
    counts = rc.replay_counts(DAY, DAY, db_path=db)
    assert counts["NATURALGAS"].get(DAY.isoformat(), 0) >= 1
    assert sum(counts["GOLD"].values()) == 0     # no data, no noise


def test_divergence_alerts_when_live_logged_nothing(db, monkeypatch):
    sent = []
    monkeypatch.setattr(rc, "send_message",
                        lambda t, *a, **k: sent.append(t) or True)
    _seed_breakout_series(db, SEED_DAYS)

    result = rc.compare(DAY, DAY, db_path=db)

    assert result["diverged"], "replay>0 with empty decision_log must flag"
    assert any("DIVERGENCE" in t for t in sent)
    assert any("NATURALGAS" in line for line in result["diverged"])
    with models._conn(db) as c:
        rows = c.execute("SELECT symbol, replay_candidates, live_candidates,"
                         " diverged FROM replay_checks "
                         "WHERE symbol='NATURALGAS'").fetchall()
    assert rows and rows[0][3] == 1              # history persisted


def test_no_alert_when_live_matches(db, monkeypatch):
    sent = []
    monkeypatch.setattr(rc, "send_message",
                        lambda t, *a, **k: sent.append(t) or True)
    _seed_breakout_series(db, SEED_DAYS)
    replay_n = sum(rc.replay_counts(DAY, DAY, db_path=db)
                   ["NATURALGAS"].values())
    _insert_pm_rows(db, DAY, replay_n)           # live saw the same

    result = rc.compare(DAY, DAY, db_path=db)
    assert not result["diverged"]
    assert not sent


def test_ratio_tolerance(db, monkeypatch):
    from config import settings
    sent = []
    monkeypatch.setattr(rc, "send_message",
                        lambda t, *a, **k: sent.append(t) or True)
    monkeypatch.setattr(settings, "REPLAY_ALERT_RATIO", 3.0)
    _seed_breakout_series(db, SEED_DAYS)
    replay_n = sum(rc.replay_counts(DAY, DAY, db_path=db)
                   ["NATURALGAS"].values())
    assert replay_n >= 4, "seed must produce enough signals for ratio test"
    _insert_pm_rows(db, DAY, 1)                  # live saw only one

    result = rc.compare(DAY, DAY, db_path=db)    # ratio replay/1 > 3.0
    assert result["diverged"] and sent


def test_livefeed_fetch_pacing(monkeypatch):
    """Broker candle fetches are spaced CANDLE_FETCH_GAP_SECS apart no
    matter how fast the scan loop calls (rate-limit guard, 2026-07-17)."""
    from config import settings
    from data.feed import LiveFeed

    monkeypatch.setattr(settings, "CANDLE_FETCH_GAP_SECS", 1.5)
    clock = {"t": 100.0}
    naps: list[float] = []

    import time as _time
    monkeypatch.setattr(_time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(_time, "sleep",
                        lambda s: (naps.append(s),
                                   clock.__setitem__("t", clock["t"] + s)))

    LiveFeed._last_fetch_mono = 0.0
    LiveFeed._pace()                 # first call: no wait
    assert naps == []
    LiveFeed._pace()                 # immediate second call: full gap
    assert naps and abs(naps[0] - 1.5) < 1e-9
    clock["t"] += 10.0               # plenty of time passes
    LiveFeed._pace()                 # no extra sleep needed
    assert len(naps) == 1
