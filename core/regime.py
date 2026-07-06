"""Market regime detection — the master gate (mcx-regime-detector skill).

No trend strategy runs in a ranging market; mean-reversion only runs when
the regime is explicitly ranging. Thresholds are module constants so the
Reflection agent (step 8) can adapt them within logged, bounded limits.
"""

import math
from dataclasses import dataclass

import pandas as pd

from config.symbols import ATR_LIMITS
from core import indicators as ind

ADX_STRONG = 30.0
ADX_TRENDING = 25.0
ADX_RANGING = 20.0
BB_WIDE = 2.0       # % — expansion confirming a strong trend
BB_SQUEEZE = 1.5    # % — compression preceding a breakout
MIN_BARS = 50


@dataclass
class Regime:
    regime: str        # STRONG_TREND / TRENDING / SQUEEZE / NEUTRAL / RANGING / UNKNOWN
    direction: str     # BULLISH / BEARISH
    adx: float
    dmp: float
    dmn: float
    atr: float
    atr_pct: float
    bb_width: float
    strategy: str      # human-readable recommendation
    can_trade: bool


def classify_regime(df: pd.DataFrame) -> Regime:
    """Classify one timeframe. Needs >= MIN_BARS candles (warmup)."""
    if len(df) < MIN_BARS:
        return Regime("UNKNOWN", "NEUTRAL", 0, 0, 0, 0, 0, 0,
                      f"HOLD — need {MIN_BARS}+ bars", False)

    adx_df = ind.adx(df)
    adx_v = float(adx_df["adx"].iloc[-1])
    dmp = float(adx_df["dmp"].iloc[-1])
    dmn = float(adx_df["dmn"].iloc[-1])
    atr_v = float(ind.atr(df).iloc[-1])
    atr_pct = atr_v / float(df["close"].iloc[-1]) * 100
    bb_width = float(ind.bollinger(df["close"])["width_pct"].iloc[-1])

    if any(math.isnan(x) for x in (adx_v, atr_v, bb_width)):
        return Regime("UNKNOWN", "NEUTRAL", 0, 0, 0, atr_v, atr_pct, 0,
                      "HOLD — indicators not ready", False)

    if adx_v > ADX_STRONG and bb_width > BB_WIDE:
        regime, strategy, trade = "STRONG_TREND", "Supertrend", True
    elif adx_v > ADX_TRENDING:
        regime, strategy, trade = "TRENDING", "Supertrend or ORB", True
    elif adx_v > ADX_RANGING and bb_width < BB_SQUEEZE:
        regime, strategy, trade = "SQUEEZE", "ORB or wait", True
    elif adx_v < ADX_RANGING:
        regime, strategy, trade = "RANGING", "HOLD — no trend strategy", False
    else:
        regime, strategy, trade = "NEUTRAL", "ORB only", True

    return Regime(regime, "BULLISH" if dmp > dmn else "BEARISH",
                  round(adx_v, 1), round(dmp, 1), round(dmn, 1),
                  round(atr_v, 2), round(atr_pct, 2), round(bb_width, 2),
                  strategy, trade)


def volatility_ok(symbol: str, atr_pct: float) -> tuple[bool, str]:
    """Veto when volatility is outside the per-symbol ATR band."""
    limits = ATR_LIMITS.get(symbol, {"min": 0.2, "max": 3.0})
    if atr_pct < limits["min"]:
        return False, (f"Volatility too LOW ({atr_pct:.2f}% < "
                       f"{limits['min']}%)")
    if atr_pct > limits["max"]:
        return False, (f"Volatility too HIGH ({atr_pct:.2f}% > "
                       f"{limits['max']}%)")
    return True, "Volatility OK"


def mtf_regime(feed, symbol: str,
               timeframes: tuple[str, ...] = ("FIFTEEN_MINUTE", "ONE_HOUR"),
               lookback: int = 200) -> dict:
    """Multi-timeframe consensus: trade only when a strict majority of
    timeframes allow it (the skill's 2-of-3 rule, generalised)."""
    results = {tf: classify_regime(feed.get_candles(symbol, tf, lookback))
               for tf in timeframes}

    tradeable = sum(1 for r in results.values() if r.can_trade)
    majority = len(timeframes) // 2 + 1
    directions = [r.direction for r in results.values()]
    dominant = max(set(directions), key=directions.count)

    return {
        "timeframes": results,
        "consensus": tradeable >= majority,
        "direction": dominant,
        "confidence": f"{tradeable}/{len(timeframes)} timeframes tradeable",
    }
