---
name: mcx-signal-analyzer
description: >
  Analyze MCX commodity trading signals using Supertrend, ORB, EMA crossover,
  and ADX regime filters. Use this skill whenever the user asks about trade
  signals, entry/exit points, strategy evaluation, indicator values, whether
  to BUY or SELL a commodity, or wants to know if conditions are right to trade.
  Also triggers for questions like "should I enter now?", "what's the signal
  for crude?", "is gold trending?", or any multi-timeframe analysis request.
---

# MCX Signal Analyzer

Generates and evaluates trading signals for MCX commodities using a layered
strategy stack. Always apply regime filter first — never output BUY/SELL in
a ranging market.

## Signal Generation Order

1. **Regime Filter** — Is the market trending or ranging?
2. **Primary Signal** — Supertrend (trend) or ORB (morning session)
3. **MTF Confirmation** — Does 1H agree with 15min signal?
4. **Volume Confirm** — Is volume above 1.5x 20-period average?
5. **RR Check** — Is reward:risk >= 2.0?
6. **Output** — BUY / SELL / HOLD with levels

## Regime Filter Rules

```python
# ADX > 25 = TRENDING -> use Supertrend or MTF EMA
# ADX 20-25 = NEUTRAL -> only ORB allowed
# ADX < 20 = RANGING -> output HOLD, no trade
import pandas_ta as ta

def get_regime(df):
    adx = ta.adx(df['high'], df['low'], df['close'], length=14)
    val = adx['ADX_14'].iloc[-1]
    if val > 25: return "TRENDING"
    if val < 20: return "RANGING"
    return "NEUTRAL"
```

## The 5-Instrument Lineup (Trade Simultaneously)

The bot trades 5 MCX commodities at once, each with a strategy matched to how
that market actually behaves — the same philosophy as running mean-reversion on
indices vs. momentum on crypto, but mapped to commodities.

| Instrument   | Character                              | Primary Strategy                        | Timeframe | Cluster        |
|--------------|----------------------------------------|-----------------------------------------|-----------|----------------|
| CRUDEOIL     | Most liquid, news-driven, strong AM moves | ORB (morning) + Supertrend (trend)   | 15min     | ENERGY         |
| NATURALGAS   | Very volatile, sharp momentum spikes   | Momentum breakout (Donchian 20 + volume)| 15min-1H  | ENERGY         |
| GOLD         | Clean, sustained trends, low noise     | Trend following (50/200 EMA crossover)  | 1H / 4H   | PRECIOUS_METAL |
| SILVER       | Higher-beta gold, overextends & reverts| Supertrend in trend, mean-revert in range| 15min-1H | PRECIOUS_METAL |
| COPPER       | Industrial demand, moderate trends     | Trend following (Supertrend / EMA)      | 1H        | BASE_METAL     |

Rules that apply across all 5:
- **ADX regime filter gates every instrument** — no trend strategy runs when its
  own instrument is ranging; mean-reversion (SILVER) only runs when ranging.
- **Correlation filter runs across the lineup** — don't hold same-direction risk
  in both energy names, or both precious metals, at once (see
  `mcx-correlation-filter`). With 5 instruments this matters a lot.
- **Each instrument keeps its own position, stop, and monitor** — they trade
  independently but share the portfolio-level drawdown circuit breaker.
- **Capital-efficient alternative:** if the account is small, swap the standard
  contracts for MCX mini variants — CRUDEOILM, NATURALGASM, GOLDM (100g),
  SILVERM (5kg) — same strategies, much lower margin per lot. Make this a config
  toggle.

Per-instrument strategy assignment should be config-driven, e.g.:

```python
INSTRUMENT_STRATEGY = {
    "CRUDEOIL"   : {"primary": "orb_then_supertrend", "tf": "15minute"},
    "NATURALGAS" : {"primary": "momentum_breakout",   "tf": "15minute"},
    "GOLD"       : {"primary": "ema_trend",           "tf": "60minute"},
    "SILVER"     : {"primary": "supertrend_or_meanrev","tf": "15minute"},
    "COPPER"     : {"primary": "supertrend",          "tf": "60minute"},
}
```

## Strategy Selection by Time (IST)

| Time Window     | Strategy      | Condition           |
|-----------------|---------------|---------------------|
| 09:00 - 09:30   | Wait          | Building OR range   |
| 09:30 - 14:00   | ORB           | Morning session     |
| 14:00 - 21:00   | Supertrend    | ADX > 25 required   |
| 21:00 - 23:25   | Supertrend    | High volatility     |
| 23:25 - 23:30   | EXIT ONLY     | Pre-close squareoff |


## Supertrend Signal Logic

```python
def supertrend_signal(df, period=10, mult=3.0):
    st = ta.supertrend(df['high'], df['low'], df['close'],
                       length=period, multiplier=mult)
    col = f'SUPERTd_{period}_{mult}'
    curr = st[col].iloc[-1]
    prev = st[col].iloc[-2]
    if curr == 1 and prev == -1: return "BUY"   # Bullish flip
    if curr == -1 and prev == 1: return "SELL"  # Bearish flip
    return "HOLD"
```

## ORB Signal Logic

```python
from datetime import time as t

def orb_signal(df, minutes=30):
    or_candles = df[df.index.time < t(9, 30)]
    or_high = or_candles['high'].max()
    or_low  = or_candles['low'].min()
    price   = df['close'].iloc[-1]
    avg_vol = df['volume'].iloc[-20:].mean()
    vol_ok  = df['volume'].iloc[-1] > avg_vol * 1.5

    if price > or_high and vol_ok: return "BUY", or_high, or_low
    if price < or_low  and vol_ok: return "SELL", or_high, or_low
    return "HOLD", or_high, or_low
```

## Stop Loss & Target Calculation

Always use ATR-based stops. Never use fixed-point stops.

```python
def get_levels(df, signal, atr_sl_mult=1.5, rr=2.0):
    atr   = ta.atr(df['high'], df['low'], df['close']).iloc[-1]
    entry = df['close'].iloc[-1]
    if signal == "BUY":
        sl     = entry - atr_sl_mult * atr
        target = entry + rr * abs(entry - sl)
    else:
        sl     = entry + atr_sl_mult * atr
        target = entry - rr * abs(entry - sl)
    return {"entry": entry, "sl": sl, "target": target,
            "rr": rr, "atr": atr}
```

## Signal Output Format

Always respond with this structure when outputting a signal:

```
📊 SIGNAL: [BUY/SELL/HOLD]
Symbol   : CRUDEOIL / GOLD / SILVER / NATURALGAS
Strategy : Supertrend / ORB / HOLD (reason)
Regime   : TRENDING / RANGING / NEUTRAL (ADX: XX)
Timeframe: 15min + 1H confirmation

Entry    : ₹XXXX
Stop Loss: ₹XXXX  (-X.X% | X.X ATR)
Target   : ₹XXXX  (+X.X% | RR: 2.0)
Lot Size : X lots (₹XXX risk)

⚠️ Notes: [volume confirmation / MTF disagreement / near expiry]
```

## What NEVER to Signal

- BUY/SELL when ADX < 20
- Any signal within 5 minutes of 23:25 IST (squareoff zone)
- Any signal if daily loss limit is hit (check with risk manager)
- Signals on expiry day without explicit user confirmation
- Signals with RR < 2.0
