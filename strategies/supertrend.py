"""Supertrend(10, 3) flip with 1H agreement — the trend engine."""

from datetime import datetime

import pandas as pd

from core import indicators as ind
from strategies.base import Signal, Strategy, make_signal


class SupertrendStrategy(Strategy):
    name = "supertrend"

    def __init__(self, period: int = 10, mult: float = 3.0):
        self.period = period
        self.mult = mult

    def generate(self, df15: pd.DataFrame, df1h: pd.DataFrame,
                 regime, now: datetime) -> Signal:
        if len(df15) < 50:
            return Signal.hold(self.name, "warmup: not enough 15m bars")

        st = ind.supertrend(df15, self.period, self.mult)
        curr, prev = int(st["direction"].iloc[-1]), int(st["direction"].iloc[-2])
        if curr == prev:
            return Signal.hold(self.name, "no flip on last bar")
        action = "BUY" if curr == 1 else "SELL"

        # Multi-timeframe confirmation: 1H supertrend must agree.
        if len(df1h) < 50:
            return Signal.hold(self.name, "warmup: not enough 1H bars")
        h1_dir = int(ind.supertrend(df1h, self.period, self.mult)
                     ["direction"].iloc[-1])
        if (action == "BUY") != (h1_dir == 1):
            return Signal.hold(self.name,
                               f"{action} flip but 1H disagrees ({h1_dir})")

        return make_signal(df15, action, self.name,
                           f"supertrend flip {prev}->{curr}, 1H agrees")
