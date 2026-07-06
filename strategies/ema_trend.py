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
        if len(df) < self.slow + 2:
            return Signal.hold(self.name,
                               f"warmup: need {self.slow + 2}+ 1H bars")

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
