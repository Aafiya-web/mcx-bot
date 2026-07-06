"""Bollinger-band mean reversion — SILVER's ranging-market mode.

Only ever invoked when the regime is explicitly RANGING (the router
enforces this); running it in a trend is how mean-reversion accounts die.
Target is the band midpoint, stop is 1 ATR beyond entry; the RR gate in
make_signal still applies, so shallow setups are held."""

from datetime import datetime

import pandas as pd

from core import indicators as ind
from strategies.base import Signal, Strategy, get_levels, make_signal


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"

    def __init__(self, length: int = 20, std: float = 2.0,
                 atr_mult: float = 1.0):
        self.length = length
        self.std = std
        self.atr_mult = atr_mult

    def generate(self, df15: pd.DataFrame, df1h: pd.DataFrame,
                 regime, now: datetime) -> Signal:
        if len(df15) < self.length + 2:
            return Signal.hold(self.name, "warmup: not enough bars")

        bb = ind.bollinger(df15["close"], self.length, self.std)
        close = float(df15["close"].iloc[-1])
        lower = float(bb["lower"].iloc[-1])
        upper = float(bb["upper"].iloc[-1])
        mid = float(bb["mid"].iloc[-1])
        atr_v = float(ind.atr(df15).iloc[-1])

        if close < lower:
            action, sl = "BUY", close - self.atr_mult * atr_v
        elif close > upper:
            action, sl = "SELL", close + self.atr_mult * atr_v
        else:
            return Signal.hold(self.name, "inside Bollinger bands")

        levels = {"entry": close, "stop_loss": sl, "target": mid,
                  "rr": 0.0, "atr": atr_v}
        return make_signal(df15, action, self.name,
                           f"band-touch revert to mid {mid:.1f}",
                           levels=levels)
