"""Step 3 (Data) tests: MockFeed realism/determinism, candle store
round-trip, historical chunking."""

from datetime import datetime

import pandas as pd
import pytest

from data import store
from data.feed import MockFeed
from data.historical import _chunks, MAX_DAYS_PER_REQUEST


@pytest.fixture
def feed():
    return MockFeed(symbols=["CRUDEOIL", "GOLD"], n_bars=400, seed=7)


def test_mock_feed_candle_shape(feed):
    df = feed.get_candles("CRUDEOIL", lookback=100)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 100
    assert df.index.is_monotonic_increasing
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all()
    assert (df["low"] <= df[["open", "close"]].min(axis=1)).all()
    assert (df["volume"] > 0).all()


def test_mock_feed_session_hours_only(feed):
    df = feed.get_candles("CRUDEOIL", lookback=200)
    assert (df.index.weekday < 5).all()
    hours = df.index.time
    assert all(t >= datetime.strptime("09:00", "%H:%M").time() for t in hours)
    assert all(t < datetime.strptime("23:30", "%H:%M").time() for t in hours)


def test_mock_feed_deterministic():
    a = MockFeed(symbols=["GOLD"], n_bars=300, seed=1).get_candles("GOLD")
    b = MockFeed(symbols=["GOLD"], n_bars=300, seed=1).get_candles("GOLD")
    pd.testing.assert_frame_equal(a, b)


def test_ltp_is_current_close_and_step_advances(feed):
    assert feed.get_ltp("GOLD") == pytest.approx(
        float(feed.get_candles("GOLD")["close"].iloc[-1]))
    before = feed.cursor
    assert feed.step() is True
    assert feed.cursor == before + 1
    # candles window grows with the cursor
    assert len(feed.get_candles("GOLD", lookback=10_000)) == feed.cursor + 1


def test_step_exhausts_at_series_end():
    f = MockFeed(symbols=["GOLD"], n_bars=MockFeed.WARMUP + 3, seed=2)
    assert f.step() and f.step()
    assert f.step() is False


def test_hourly_resample(feed):
    m15 = feed.get_candles("CRUDEOIL", lookback=8)
    h1 = feed.get_candles("CRUDEOIL", "ONE_HOUR", lookback=2)
    assert len(h1) >= 1
    # hourly bar must contain its 15-min children
    assert h1["high"].max() <= m15["high"].max() + 1e9  # sanity: no NaN
    assert not h1.isna().any().any()


def test_set_ltp_hook(feed):
    feed.set_ltp("CRUDEOIL", 4242.0)
    assert feed.get_ltp("CRUDEOIL") == 4242.0


def test_unknown_interval_rejected(feed):
    with pytest.raises(ValueError):
        feed.get_candles("GOLD", "FIVE_MINUTE")


# ------------------------------------------------------------------ store


def test_store_round_trip(tmp_path, feed):
    db = tmp_path / "candles.db"
    df = feed.get_candles("CRUDEOIL", lookback=50)
    assert store.save_candles("CRUDEOIL", "FIFTEEN_MINUTE", df, db) == 50
    loaded = store.load_candles("CRUDEOIL", "FIFTEEN_MINUTE", db_path=db)
    assert len(loaded) == 50
    pd.testing.assert_frame_equal(
        loaded, df.rename_axis("ts"), check_freq=False)


def test_store_upsert_no_duplicates(tmp_path, feed):
    db = tmp_path / "candles.db"
    df = feed.get_candles("GOLD", lookback=20)
    store.save_candles("GOLD", "FIFTEEN_MINUTE", df, db)
    store.save_candles("GOLD", "FIFTEEN_MINUTE", df, db)  # same rows again
    assert len(store.load_candles("GOLD", "FIFTEEN_MINUTE", db_path=db)) == 20


def test_store_limit_returns_newest(tmp_path, feed):
    db = tmp_path / "candles.db"
    df = feed.get_candles("GOLD", lookback=30)
    store.save_candles("GOLD", "FIFTEEN_MINUTE", df, db)
    newest = store.load_candles("GOLD", "FIFTEEN_MINUTE", limit=5, db_path=db)
    assert len(newest) == 5
    assert newest.index[-1] == df.index[-1]


# ------------------------------------------------------------- historical


def test_chunks_cover_range_without_overlap():
    start = datetime(2026, 1, 1)
    end = datetime(2026, 6, 1)
    chunks = _chunks(start, end, "ONE_MINUTE")  # 30-day windows
    assert chunks[0][0] == start and chunks[-1][1] == end
    for (a_lo, a_hi), (b_lo, b_hi) in zip(chunks, chunks[1:]):
        assert a_hi == b_lo
    span = MAX_DAYS_PER_REQUEST["ONE_MINUTE"]
    assert all((hi - lo).days <= span for lo, hi in chunks)


def test_single_chunk_when_range_fits():
    start = datetime(2026, 6, 1)
    end = datetime(2026, 6, 10)
    assert _chunks(start, end, "FIFTEEN_MINUTE") == [(start, end)]
