"""Donchian-20 breakout with volume confirmation — NATURALGAS momentum."""

from datetime import datetime

import pandas as pd

from core import indicators as ind
from strategies.base import Signal, Strategy, make_signal


class MomentumBreakoutStrategy(Strategy):
    name = "momentum_breakout"

    def __init__(self, channel: int = 20):
        self.channel = channel

    def generate(self, df15: pd.DataFrame, df1h: pd.DataFrame,
                 regime, now: datetime) -> Signal:
        if len(df15) < self.channel + 21:
            return Signal.hold(self.name, "warmup: not enough bars")

        dc = ind.donchian(df15, self.channel)
        close = float(df15["close"].iloc[-1])
        upper = float(dc["upper"].iloc[-1])
        lower = float(dc["lower"].iloc[-1])

        if close > upper:
            action = "BUY"
        elif close < lower:
            action = "SELL"
        else:
            return Signal.hold(self.name, "inside Donchian channel")

        if not ind.volume_confirmed(df15):
            return Signal.hold(self.name,
                               "breakout without 1.5x volume — likely fake")

        return make_signal(df15, action, self.name,
                           f"Donchian-{self.channel} {action}: {close:.1f} vs "
                           f"[{lower:.1f}, {upper:.1f}], volume confirmed")
