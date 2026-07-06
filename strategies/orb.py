"""Opening Range Breakout (30-min) for the MCX morning session."""

from datetime import datetime, time as dtime

import pandas as pd

from core import indicators as ind
from strategies.base import Signal, Strategy, make_signal

OR_END = dtime(9, 30)
ORB_LAST_ENTRY = dtime(14, 0)   # morning-session strategy only


class OrbStrategy(Strategy):
    name = "orb"

    def generate(self, df15: pd.DataFrame, df1h: pd.DataFrame,
                 regime, now: datetime) -> Signal:
        t = now.time()
        if t < OR_END:
            return Signal.hold(self.name, "building opening range")
        if t >= ORB_LAST_ENTRY:
            return Signal.hold(self.name, "past ORB window (>=14:00)")

        today = df15[df15.index.date == now.date()]
        or_bars = today[today.index.time < OR_END]
        if or_bars.empty:
            return Signal.hold(self.name, "no opening-range bars today")

        or_high = float(or_bars["high"].max())
        or_low = float(or_bars["low"].min())
        price = float(df15["close"].iloc[-1])

        if not ind.volume_confirmed(df15):
            return Signal.hold(self.name, "breakout volume < 1.5x average")

        if price > or_high:
            action = "BUY"
        elif price < or_low:
            action = "SELL"
        else:
            return Signal.hold(self.name, "price inside opening range")

        return make_signal(df15, action, self.name,
                           f"ORB {action}: price {price:.1f} vs "
                           f"OR [{or_low:.1f}, {or_high:.1f}], volume ok",
                           or_high=or_high, or_low=or_low)
