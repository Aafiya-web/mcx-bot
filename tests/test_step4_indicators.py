"""Step 4 tests: indicator math against known values; regime classification
on synthetic trending/ranging data."""

import numpy as np
import pandas as pd
import pytest

from core import indicators as ind
from core.regime import classify_regime, mtf_regime, volatility_ok
from data.feed import MockFeed


def _ohlcv(closes, spread=1.0, volume=1000.0):
    closes = pd.Series(closes, dtype=float,
                       index=pd.date_range("2026-01-01", periods=len(closes),
                                           freq="15min"))
    return pd.DataFrame({
        "open": closes.shift(1).fillna(closes.iloc[0]),
        "high": closes + spread,
        "low": closes - spread,
        "close": closes,
        "volume": volume if np.isscalar(volume)
        else pd.Series(volume, index=closes.index),
    })


# -------------------------------------------------------------- indicators


def test_ema_matches_manual():
    s = pd.Series([1.0, 2.0, 3.0, 4.0])
    out = ind.ema(s, 3)  # alpha = 0.5
    assert out.iloc[1] == pytest.approx(1.5)
    assert out.iloc[2] == pytest.approx(2.25)
    assert out.iloc[3] == pytest.approx(3.125)


def test_atr_converges_to_constant_range():
    df = _ohlcv([100.0] * 200, spread=5.0)  # every bar TR = 10
    assert ind.atr(df).iloc[-1] == pytest.approx(10.0, rel=1e-6)


def test_adx_direction_on_trends():
    up = ind.adx(_ohlcv(np.linspace(100, 200, 150)))
    assert up["adx"].iloc[-1] > 25
    assert up["dmp"].iloc[-1] > up["dmn"].iloc[-1]

    down = ind.adx(_ohlcv(np.linspace(200, 100, 150)))
    assert down["adx"].iloc[-1] > 25
    assert down["dmn"].iloc[-1] > down["dmp"].iloc[-1]


def test_supertrend_follows_trend_and_flips():
    rally = np.linspace(100, 150, 100)
    crash = np.linspace(150, 100, 100)
    st = ind.supertrend(_ohlcv(np.concatenate([rally, crash])))
    assert st["direction"].iloc[99] == 1        # end of rally: uptrend
    assert st["direction"].iloc[-1] == -1       # end of crash: downtrend
    flips = st["direction"].diff().abs() == 2
    assert flips.any()                          # at least one flip occurred
    # line trails price on the correct side
    assert st["line"].iloc[99] < rally[-1]
    assert st["line"].iloc[-1] > crash[-1]


def test_bollinger_flat_series_zero_width():
    bb = ind.bollinger(pd.Series([50.0] * 40))
    assert bb["width_pct"].iloc[-1] == pytest.approx(0.0)
    assert bb["upper"].iloc[-1] == pytest.approx(50.0)


def test_donchian_uses_prior_window():
    df = _ohlcv(list(range(100, 130)), spread=0.5)
    dc = ind.donchian(df, length=20)
    # channel excludes the current bar -> a steadily rising close is always
    # above the prior 20-bar high
    assert df["close"].iloc[-1] > dc["upper"].iloc[-1]


def test_vwap_two_bars():
    df = _ohlcv([100.0, 110.0], spread=0.0, volume=[10.0, 30.0])
    v = ind.vwap(df)
    assert v.iloc[0] == pytest.approx(100.0)
    assert v.iloc[1] == pytest.approx((100 * 10 + 110 * 30) / 40)


def test_volume_confirmed():
    vols = [1000.0] * 30 + [2000.0]
    df = _ohlcv([100.0] * 31, volume=vols)
    assert ind.volume_confirmed(df) is True
    df2 = _ohlcv([100.0] * 31, volume=[1000.0] * 31)
    assert ind.volume_confirmed(df2) is False


# ------------------------------------------------------------------ regime


def test_trending_market_classified_tradeable():
    rng = np.random.default_rng(3)
    closes = np.linspace(100, 160, 200) + rng.normal(0, 0.3, 200)
    r = classify_regime(_ohlcv(closes))
    assert r.regime in ("TRENDING", "STRONG_TREND")
    assert r.direction == "BULLISH"
    assert r.can_trade is True


def test_ranging_market_vetoed():
    rng = np.random.default_rng(4)
    closes = 100 + rng.normal(0, 0.4, 300)  # pure noise
    r = classify_regime(_ohlcv(closes))
    assert r.regime == "RANGING"
    assert r.can_trade is False
    assert r.adx < 20


def test_short_history_is_unknown_and_blocked():
    r = classify_regime(_ohlcv([100.0] * 10))
    assert r.regime == "UNKNOWN"
    assert r.can_trade is False


def test_volatility_band_veto():
    ok, _ = volatility_ok("CRUDEOIL", 1.0)
    assert ok
    too_low, msg = volatility_ok("CRUDEOIL", 0.1)
    assert not too_low and "LOW" in msg
    too_high, msg = volatility_ok("CRUDEOIL", 5.0)
    assert not too_high and "HIGH" in msg


def test_mtf_regime_on_mock_feed():
    feed = MockFeed(symbols=["CRUDEOIL"], n_bars=600, seed=11)
    out = mtf_regime(feed, "CRUDEOIL")
    assert set(out["timeframes"]) == {"FIFTEEN_MINUTE", "ONE_HOUR"}
    assert isinstance(out["consensus"], bool)
    assert out["direction"] in ("BULLISH", "BEARISH")
