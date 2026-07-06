"""Per-instrument strategy routing + the regime gate.

config/symbols.py assigns each instrument a strategy KEY; this module turns
keys into runnable strategies and applies the master rule from the skills:
no trend strategy in a ranging market, mean-reversion ONLY in a ranging
market. Every HOLD carries the reason so decisions are auditable.
"""

from datetime import datetime, time as dtime

import pandas as pd

from strategies.base import Signal, Strategy
from strategies.ema_trend import EmaTrendStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.momentum_breakout import MomentumBreakoutStrategy
from strategies.orb import ORB_LAST_ENTRY, OrbStrategy
from strategies.supertrend import SupertrendStrategy

# ADX regime gate applies to every trend-following strategy.
_TREND_STRATEGIES = {"supertrend", "orb", "ema_trend", "momentum_breakout"}


class RegimeGated(Strategy):
    """Wraps a strategy with the regime rules. This wrapper is the ONLY
    path the engine uses, so the gate cannot be bypassed."""

    def __init__(self, inner: Strategy):
        self.inner = inner
        self.name = inner.name

    def generate(self, df15, df1h, regime, now: datetime) -> Signal:
        if self.inner.name in _TREND_STRATEGIES and not regime.can_trade:
            return Signal.hold(self.inner.name,
                               f"regime gate: {regime.regime} "
                               f"(ADX {regime.adx}) — no trend trades")
        if (self.inner.name == "mean_reversion"
                and regime.regime != "RANGING"):
            return Signal.hold(self.inner.name,
                               f"mean reversion only in RANGING regime "
                               f"(now {regime.regime})")
        return self.inner.generate(df15, df1h, regime, now)


class OrbThenSupertrend(Strategy):
    """CRUDEOIL: ORB during the morning window, Supertrend afterwards
    (strategy-selection-by-time table, mcx-signal-analyzer)."""

    name = "orb_then_supertrend"

    def __init__(self):
        self.orb = RegimeGated(OrbStrategy())
        self.st = RegimeGated(SupertrendStrategy())

    def generate(self, df15, df1h, regime, now: datetime) -> Signal:
        if now.time() < ORB_LAST_ENTRY:
            return self.orb.generate(df15, df1h, regime, now)
        return self.st.generate(df15, df1h, regime, now)


class SupertrendOrMeanRev(Strategy):
    """SILVER: Supertrend when trending, Bollinger mean-revert when ranging."""

    name = "supertrend_or_meanrev"

    def __init__(self):
        self.st = RegimeGated(SupertrendStrategy())
        self.mr = RegimeGated(MeanReversionStrategy())

    def generate(self, df15, df1h, regime, now: datetime) -> Signal:
        if regime.regime == "RANGING":
            return self.mr.generate(df15, df1h, regime, now)
        return self.st.generate(df15, df1h, regime, now)


def get_strategy(key: str) -> Strategy:
    """Strategy key (config/symbols.py) -> runnable, regime-gated strategy."""
    factories = {
        "supertrend": lambda: RegimeGated(SupertrendStrategy()),
        "orb": lambda: RegimeGated(OrbStrategy()),
        "ema_trend": lambda: RegimeGated(EmaTrendStrategy()),
        "momentum_breakout": lambda: RegimeGated(MomentumBreakoutStrategy()),
        "mean_reversion": lambda: RegimeGated(MeanReversionStrategy()),
        "orb_then_supertrend": OrbThenSupertrend,
        "supertrend_or_meanrev": SupertrendOrMeanRev,
    }
    if key not in factories:
        raise KeyError(f"Unknown strategy key '{key}' — valid: "
                       f"{sorted(factories)}")
    return factories[key]()
