"""50/200 EMA crossover on the higher timeframe — GOLD's clean-trend engine."""

from datetime import datetime

import pandas as pd

from core import indicators as ind
from strategies.base import Signal, Strategy, make_signal


class EmaTrendStrategy(Strategy):
    name = "ema_trend"

    def __init__(self, fast: int = 50, slow: int = 200):
        self.fast = fast
        self.slow = slow

    def generate(self, df15: pd.DataFrame, df1h: pd.DataFrame,
                 regime, now: datetime) -> Signal:
        df = df1h  # this strategy trades the higher timeframe
        # ema() uses ewm(adjust=False), seeded with the first bar, so the
        # EMA-slow is only trustworthy after ~3x its span (seed weight
        # (1-2/(slow+1))^n < ~0.25% at 3x). Below that the crossing is an
        # artifact of the seed, not the market — refuse rather than emit a
        # phantom signal (root cause of the 2026-07-24 GOLD zero-trades).
        min_bars = self.slow * 3
        if len(df) < min_bars:
            return Signal.hold(self.name,
                               f"warmup: need {min_bars}+ 1H bars "
                               f"for EMA{self.slow} to converge "
                               f"(have {len(df)})")

        diff = ind.ema(df["close"], self.fast) - ind.ema(df["close"], self.slow)
        prev, curr = float(diff.iloc[-2]), float(diff.iloc[-1])

        if prev <= 0 < curr:
            action = "BUY"
        elif prev >= 0 > curr:
            action = "SELL"
        else:
            return Signal.hold(self.name, "no 50/200 cross on last 1H bar")

        return make_signal(df, action, self.name,
                           f"golden/death cross: EMA{self.fast} vs "
                           f"EMA{self.slow} ({prev:.2f} -> {curr:.2f})")
