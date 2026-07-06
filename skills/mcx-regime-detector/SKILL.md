---
name: mcx-regime-detector
description: >
  Detects market regime (trending, ranging, volatile) for MCX commodities
  using ADX, ATR, and volatility filters to prevent trading in unfavorable
  conditions. Use this skill when the user asks if market conditions are good
  for trading, whether a commodity is trending or sideways, what the ADX is,
  if volatility is too high or low, or whether to skip trading today. Also
  triggers for "is crude trending?", "why did the bot not trade?", "should
  I trade in these conditions?", or any question about market environment.
---

# MCX Regime Detector

The most important pre-trade filter. Identifies market conditions and
selects the appropriate strategy. Prevents trend strategies from running
in choppy markets — the #1 cause of bot losses.

## Regime Classification

```python
import pandas_ta as ta
import pandas as pd

def classify_regime(df) -> dict:
    """
    Returns regime type + recommended strategy + confidence score.
    Run this on 15min data with at least 50 candles.
    """
    # ADX for trend strength
    adx_data = ta.adx(df['high'], df['low'], df['close'], length=14)
    adx      = adx_data['ADX_14'].iloc[-1]
    dmp      = adx_data['DMP_14'].iloc[-1]   # +DI
    dmn      = adx_data['DMN_14'].iloc[-1]   # -DI

    # ATR for volatility
    atr      = ta.atr(df['high'], df['low'], df['close'], length=14).iloc[-1]
    atr_pct  = atr / df['close'].iloc[-1] * 100  # ATR as % of price

    # Bollinger Band width for squeeze detection
    bb       = ta.bbands(df['close'], length=20)
    bb_width = (bb['BBU_20_2.0'] - bb['BBL_20_2.0']).iloc[-1] / \
                bb['BBM_20_2.0'].iloc[-1] * 100

    # Classify
    if adx > 30 and bb_width > 2.0:
        regime    = "STRONG_TREND"
        strategy  = "Supertrend"
        trade     = True
    elif adx > 25:
        regime    = "TRENDING"
        strategy  = "Supertrend or ORB"
        trade     = True
    elif adx > 20 and bb_width < 1.5:
        regime    = "SQUEEZE"      # Breakout incoming
        strategy  = "ORB or wait"
        trade     = True  # With caution
    elif adx < 20:
        regime    = "RANGING"
        strategy  = "HOLD — no trend strategy"
        trade     = False
    else:
        regime    = "NEUTRAL"
        strategy  = "ORB only"
        trade     = True  # Morning session only

    direction = "BULLISH" if dmp > dmn else "BEARISH"

    return {
        "regime"    : regime,
        "direction" : direction,
        "adx"       : round(adx, 1),
        "dmp"       : round(dmp, 1),
        "dmn"       : round(dmn, 1),
        "atr"       : round(atr, 2),
        "atr_pct"   : round(atr_pct, 2),
        "bb_width"  : round(bb_width, 2),
        "strategy"  : strategy,
        "can_trade" : trade,
    }
```

## Volatility Filter

```python
# ATR % thresholds per symbol (based on typical ranges)
ATR_LIMITS = {
    "CRUDEOIL"   : {"min": 0.3, "max": 3.0},  # % of price
    "GOLD"       : {"min": 0.2, "max": 1.5},
    "SILVER"     : {"min": 0.3, "max": 2.5},
    "NATURALGAS" : {"min": 0.5, "max": 5.0},  # More volatile
}

def volatility_ok(symbol, atr_pct) -> tuple[bool, str]:
    limits = ATR_LIMITS.get(symbol, {"min": 0.2, "max": 3.0})
    if atr_pct < limits['min']:
        return False, f"Volatility too LOW ({atr_pct:.2f}% < {limits['min']}%)"
    if atr_pct > limits['max']:
        return False, f"Volatility too HIGH ({atr_pct:.2f}% > {limits['max']}%)"
    return True, "Volatility OK"
```

## Multi-Timeframe Regime Check

```python
def mtf_regime(api, symbol_token):
    """
    Check regime on 3 timeframes. Trade only when 2/3 agree.
    """
    from data.historical import fetch_ohlcv
    results = {}
    for tf in ["15minute", "60minute", "ONE_DAY"]:
        df = fetch_ohlcv(api, symbol_token, tf, days=30)
        results[tf] = classify_regime(df)

    trending_count = sum(
        1 for r in results.values() if r['can_trade']
    )
    dominant_direction = max(
        set(r['direction'] for r in results.values()),
        key=lambda d: sum(1 for r in results.values()
                          if r['direction'] == d)
    )

    return {
        "timeframes"  : results,
        "consensus"   : trending_count >= 2,
        "direction"   : dominant_direction,
        "confidence"  : f"{trending_count}/3 timeframes agree",
    }
```

## Regime Output Format

```
🛡️ MARKET REGIME: CRUDEOIL
------------------------------
Regime    : STRONG_TREND ✅
Direction : BULLISH (DI+: 28.4 > DI-: 12.1)
ADX       : 34.2  (>30 = strong trend)
ATR       : ₹42.5 (1.2% of price ✅)
BB Width  : 3.1%  (expanding ✅)

15min     : TRENDING ✅
1H        : STRONG_TREND ✅
Daily     : TRENDING ✅
Consensus : 3/3 timeframes agree

Recommended: Supertrend BUY side
Can Trade : YES ✅
------------------------------
```

## When to Skip Trading (output HOLD)

- ADX < 20 on primary timeframe
- Volatility outside ATR limits
- Less than 2/3 timeframes agree on direction
- News event within 30 minutes (OPEC, Fed, CPI)
- Contract expiry day
- First 30 minutes of session (9:00-9:30)
- Last 5 minutes of session (23:25-23:30)

## News Event Calendar (MCX Impact)

High-impact events that override regime and force HOLD:

```python
HIGH_IMPACT_EVENTS = {
    "CRUDEOIL"   : ["OPEC meeting", "EIA inventory", "Fed rate decision",
                    "US CPI", "US NFP"],
    "GOLD"       : ["Fed rate decision", "US CPI", "US NFP", "DXY spike"],
    "SILVER"     : ["Fed rate decision", "Industrial output data"],
    "NATURALGAS" : ["EIA gas storage", "Weather forecasts"],
}
# Integrate with economic calendar API (e.g. Forex Factory RSS)
# or manually note in Telegram before each session
```
