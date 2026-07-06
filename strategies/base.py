"""Strategy contract + shared discipline rules (mcx-signal-analyzer skill).

Every strategy returns a Signal. Discipline enforced HERE so no individual
strategy can forget it:
- stops are always ATR-based (get_levels), never fixed points
- reward:risk below settings.MIN_REWARD_RISK is demoted to HOLD
"""

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from config import settings
from core import indicators as ind


@dataclass
class Signal:
    action: str                 # BUY / SELL / HOLD
    strategy: str = ""
    entry: float = 0.0
    stop_loss: float = 0.0
    target: float = 0.0
    rr: float = 0.0
    atr: float = 0.0
    reason: str = ""
    extras: dict = field(default_factory=dict)

    @classmethod
    def hold(cls, strategy: str, reason: str) -> "Signal":
        return cls("HOLD", strategy=strategy, reason=reason)


def get_levels(df: pd.DataFrame, action: str,
               atr_mult: float | None = None,
               rr: float | None = None) -> dict:
    """ATR stop + RR-multiple target from the last close."""
    atr_mult = atr_mult if atr_mult is not None else settings.ATR_SL_MULT
    rr = rr if rr is not None else settings.MIN_REWARD_RISK
    atr_v = float(ind.atr(df).iloc[-1])
    entry = float(df["close"].iloc[-1])
    if action == "BUY":
        sl = entry - atr_mult * atr_v
        target = entry + rr * (entry - sl)
    else:
        sl = entry + atr_mult * atr_v
        target = entry - rr * (sl - entry)
    return {"entry": entry, "stop_loss": sl, "target": target,
            "rr": rr, "atr": atr_v}


def make_signal(df: pd.DataFrame, action: str, strategy: str, reason: str,
                levels: dict | None = None, **extras) -> Signal:
    """Build a BUY/SELL signal with levels; demote to HOLD if RR is short."""
    if action == "HOLD":
        return Signal.hold(strategy, reason)
    lv = levels or get_levels(df, action)
    risk = abs(lv["entry"] - lv["stop_loss"])
    rr = abs(lv["target"] - lv["entry"]) / risk if risk > 0 else 0.0
    if rr < settings.MIN_REWARD_RISK:
        return Signal.hold(strategy,
                           f"RR {rr:.2f} < {settings.MIN_REWARD_RISK} — {reason}")
    return Signal(action, strategy, lv["entry"], lv["stop_loss"],
                  lv["target"], round(rr, 2), lv["atr"], reason, extras)


class Strategy:
    """Interface. df15 = primary timeframe candles, df1h = higher timeframe
    bias, regime = core.regime.Regime for the instrument, now = session
    clock (feed time, so mock runs are faithful)."""

    name = "base"

    def generate(self, df15: pd.DataFrame, df1h: pd.DataFrame,
                 regime, now: datetime) -> Signal:
        raise NotImplementedError
