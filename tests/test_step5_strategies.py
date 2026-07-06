"""Step 5 tests: each strategy fires on crafted data, the regime gate and
RR discipline hold, and the router picks correctly by time/regime."""

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from config.symbols import INSTRUMENTS
from core.regime import Regime
from strategies.base import make_signal
from strategies.ema_trend import EmaTrendStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.momentum_breakout import MomentumBreakoutStrategy
from strategies.orb import OrbStrategy
from strategies.router import (OrbThenSupertrend, RegimeGated,
                               SupertrendOrMeanRev, get_strategy)
from strategies.supertrend import SupertrendStrategy


def _frame(closes, spread=1.0, volume=None, start="2026-07-06 09:00"):
    n = len(closes)
    closes = pd.Series(np.asarray(closes, dtype=float),
                       index=pd.date_range(start, periods=n, freq="15min"))
    return pd.DataFrame({
        "open": closes.shift(1).fillna(closes.iloc[0]),
        "high": closes + spread,
        "low": closes - spread,
        "close": closes,
        "volume": (np.full(n, 1000.0) if volume is None
                   else np.asarray(volume, dtype=float)),
    })


TRENDING = Regime("TRENDING", "BULLISH", 30, 25, 10, 5, 0.8, 2.5,
                  "Supertrend", True)
RANGING = Regime("RANGING", "NEUTRAL", 15, 12, 11, 5, 0.5, 1.0,
                 "HOLD", False)
NOW = datetime(2026, 7, 6, 15, 0)


# -------------------------------------------------------------- supertrend


def _flip_frames():
    """15m frame whose LAST bar is a supertrend flip to BUY, plus an
    agreeing (rising) 1H frame."""
    from core.indicators import supertrend
    closes = np.concatenate([np.linspace(150, 100, 120),
                             np.linspace(100, 130, 80)])
    df = _frame(closes)
    st = supertrend(df)
    flips = np.where(st["direction"].diff().abs() == 2)[0]
    buy_flips = [i for i in flips if st["direction"].iloc[i] == 1 and i > 60]
    i = buy_flips[-1]
    df15 = df.iloc[: i + 1]
    df1h = _frame(np.linspace(100, 140, 80))  # 1H uptrend -> agrees
    return df15, df1h


def test_supertrend_buy_flip_with_mtf_agreement():
    df15, df1h = _flip_frames()
    sig = SupertrendStrategy().generate(df15, df1h, TRENDING, NOW)
    assert sig.action == "BUY"
    assert sig.stop_loss < sig.entry < sig.target
    assert sig.rr >= 2.0


def test_supertrend_flip_blocked_when_1h_disagrees():
    df15, _ = _flip_frames()
    df1h_down = _frame(np.linspace(140, 100, 80))
    sig = SupertrendStrategy().generate(df15, df1h_down, TRENDING, NOW)
    assert sig.action == "HOLD"
    assert "1H disagrees" in sig.reason


def test_supertrend_no_flip_holds():
    df = _frame(np.linspace(100, 130, 100))  # steady trend, no last-bar flip
    sig = SupertrendStrategy().generate(df, df, TRENDING, NOW)
    assert sig.action == "HOLD"


# --------------------------------------------------------------------- orb


def _orb_day(breakout_close, breakout_volume=2500.0):
    """Prior-day bars for volume history + today's 09:00/09:15 range bars
    (100-102) + a 09:30 breakout bar."""
    prior = _frame([100.0] * 30, spread=0.5, start="2026-07-03 09:00")
    today = _frame([101.0, 101.5, breakout_close], spread=0.5,
                   start="2026-07-06 09:00",
                   volume=[1000.0, 1000.0, breakout_volume])
    return pd.concat([prior, today])


def test_orb_breakout_buy():
    df = _orb_day(breakout_close=104.0)
    sig = OrbStrategy().generate(df, df, TRENDING,
                                 datetime(2026, 7, 6, 9, 45))
    assert sig.action == "BUY"
    assert sig.extras["or_high"] == pytest.approx(102.0)
    assert sig.extras["or_low"] == pytest.approx(100.5)


def test_orb_needs_volume():
    df = _orb_day(breakout_close=104.0, breakout_volume=1000.0)
    sig = OrbStrategy().generate(df, df, TRENDING,
                                 datetime(2026, 7, 6, 9, 45))
    assert sig.action == "HOLD" and "volume" in sig.reason


def test_orb_respects_time_window():
    df = _orb_day(104.0)
    building = OrbStrategy().generate(df, df, TRENDING,
                                      datetime(2026, 7, 6, 9, 15))
    assert building.action == "HOLD" and "building" in building.reason
    late = OrbStrategy().generate(df, df, TRENDING,
                                  datetime(2026, 7, 6, 15, 0))
    assert late.action == "HOLD" and "past ORB" in late.reason


# --------------------------------------------------------------- ema trend


def test_ema_cross_fires_buy():
    from core.indicators import ema
    closes = np.concatenate([np.linspace(120, 100, 260),
                             np.linspace(100, 118, 120)])
    df = _frame(closes)
    diff = ema(df["close"], 50) - ema(df["close"], 200)
    cross = np.where((diff.shift(1) <= 0) & (diff > 0))[0]
    df1h = df.iloc[: cross[-1] + 1]
    sig = EmaTrendStrategy().generate(df1h, df1h, TRENDING, NOW)
    assert sig.action == "BUY"
    assert "cross" in sig.reason


def test_ema_no_cross_holds():
    df = _frame(np.linspace(100, 140, 260))
    sig = EmaTrendStrategy().generate(df, df, TRENDING, NOW)
    assert sig.action == "HOLD"


# ------------------------------------------------------- momentum breakout


def test_donchian_breakout_with_volume():
    closes = [100.0] * 40 + [105.0]
    vols = [1000.0] * 40 + [2500.0]
    df = _frame(closes, spread=0.4, volume=vols)
    sig = MomentumBreakoutStrategy().generate(df, df, TRENDING, NOW)
    assert sig.action == "BUY"


def test_donchian_breakout_without_volume_held():
    closes = [100.0] * 40 + [105.0]
    df = _frame(closes, spread=0.4)
    sig = MomentumBreakoutStrategy().generate(df, df, TRENDING, NOW)
    assert sig.action == "HOLD" and "volume" in sig.reason


# ----------------------------------------------------------- mean reversion


def test_band_touch_reverts():
    closes = list(100 + np.random.default_rng(5).normal(0, 0.3, 40)) + [95.0]
    df = _frame(closes, spread=0.4)
    sig = MeanReversionStrategy().generate(df, df, RANGING, NOW)
    assert sig.action == "BUY"
    assert sig.target < 101  # mid band, not a trend target
    assert sig.rr >= 2.0


def test_inside_bands_holds():
    df = _frame(100 + np.random.default_rng(6).normal(0, 0.3, 40))
    sig = MeanReversionStrategy().generate(df, df, RANGING, NOW)
    assert sig.action == "HOLD"


# ------------------------------------------------------------ gate + router


def test_regime_gate_blocks_trend_strategy_in_range():
    df15, df1h = _flip_frames()
    sig = RegimeGated(SupertrendStrategy()).generate(df15, df1h, RANGING, NOW)
    assert sig.action == "HOLD" and "regime gate" in sig.reason


def test_regime_gate_blocks_meanrev_in_trend():
    closes = list(100 + np.random.default_rng(5).normal(0, 0.3, 40)) + [95.0]
    df = _frame(closes)
    sig = RegimeGated(MeanReversionStrategy()).generate(df, df, TRENDING, NOW)
    assert sig.action == "HOLD" and "RANGING" in sig.reason


def test_rr_discipline_demotes_to_hold():
    df = _frame([100.0] * 60)
    levels = {"entry": 100.0, "stop_loss": 98.0, "target": 101.0,
              "rr": 0.5, "atr": 2.0}
    sig = make_signal(df, "BUY", "x", "shallow", levels=levels)
    assert sig.action == "HOLD" and "RR" in sig.reason


def test_router_time_split_crudeoil():
    router = OrbThenSupertrend()
    df = _orb_day(104.0)
    morning = router.generate(df, df, TRENDING, datetime(2026, 7, 6, 9, 45))
    assert morning.strategy == "orb" and morning.action == "BUY"
    afternoon = router.generate(df, df, TRENDING, datetime(2026, 7, 6, 15, 0))
    assert afternoon.strategy == "supertrend"


def test_router_regime_split_silver():
    router = SupertrendOrMeanRev()
    closes = list(100 + np.random.default_rng(5).normal(0, 0.3, 40)) + [95.0]
    df = _frame(closes)
    ranging = router.generate(df, df, RANGING, NOW)
    assert ranging.strategy == "mean_reversion" and ranging.action == "BUY"
    trending = router.generate(df, df, TRENDING, NOW)
    assert trending.strategy == "supertrend"


def test_every_configured_strategy_key_resolves():
    for name, info in INSTRUMENTS.items():
        strat = get_strategy(info["strategy"])
        assert strat is not None, name


def test_unknown_key_raises():
    with pytest.raises(KeyError):
        get_strategy("hopium")
