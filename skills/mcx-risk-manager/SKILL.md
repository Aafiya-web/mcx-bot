---
name: mcx-risk-manager
description: >
  Manages position sizing, daily loss limits, reward:risk validation, and
  capital allocation for MCX commodity trading. Use this skill whenever the
  user asks how many lots to trade, whether to take a trade based on risk,
  what the stop loss should be, how much capital to risk, whether the daily
  loss limit is hit, or anything about money management, drawdown, or position
  sizing. Also triggers for "how much should I risk?", "is this trade worth
  it?", "what's my max loss today?", or reviewing P&L vs risk parameters.
---

# MCX Risk Manager

Enforces risk rules before every trade. Acts as the final gate — a signal
from the analyzer only becomes a trade if the risk manager approves it.

## Core Risk Parameters

```python
# From config/settings.py — adjust these only deliberately
# NOTE (guardrail #4): INITIAL_CAPITAL, MAX_DAILY_LOSS_PCT and MAX_DRAWDOWN_PCT
# are None placeholders in the real config; the 100_000 / 2.0 below are
# ILLUSTRATIVE EXAMPLES ONLY. The system refuses LIVE mode while unset.
INITIAL_CAPITAL      = 100_000   # ₹ (example — real config uses None)
MAX_RISK_PER_TRADE   = 1.0       # % of capital per trade
MAX_DAILY_LOSS_PCT   = 2.0       # Hard stop for the day (example)
MAX_TRADES_PER_DAY   = 5         # Prevent overtrading
MIN_REWARD_RISK      = 2.0       # Minimum RR to enter
MAX_POSITION_PCT     = 10.0      # Max % of capital in one position
```

## MCX Lot Sizes (Required for Position Sizing)

```python
LOT_SIZES = {
    "CRUDEOIL"   : 100,    # barrels
    "GOLD"       : 1,      # kg
    "GOLDMINI"   : 100,    # grams
    "SILVER"     : 30,     # kg
    "SILVERMINI" : 5,      # kg
    "NATURALGAS" : 1250,   # mmBtu
    "COPPER"     : 2500,   # kg
    "ZINC"       : 5000,   # kg
    "ALUMINIUM"  : 5000,   # kg
}
```

## Position Sizing Formula

```python
def position_size(capital, symbol, entry, stop_loss, risk_pct=1.0):
    """
    How many lots can we trade given our risk tolerance?
    Risk Amount = Capital x Risk%
    Lots = Risk Amount / (SL Points x Lot Size)
    """
    lot_size    = LOT_SIZES[symbol]
    risk_amount = capital * risk_pct / 100
    sl_points   = abs(entry - stop_loss)

    if sl_points == 0:
        return 0

    raw_lots = risk_amount / (sl_points * lot_size)
    lots     = max(1, int(raw_lots))  # Minimum 1 lot

    # Sanity check: position value must not exceed MAX_POSITION_PCT
    position_value = lots * lot_size * entry
    if position_value > capital * MAX_POSITION_PCT / 100:
        lots = int((capital * MAX_POSITION_PCT / 100) / (lot_size * entry))
        lots = max(1, lots)

    return lots
```

## Pre-Trade Checklist

Run ALL checks before approving a trade:

```
[ ] 1. Daily P&L > -MAX_DAILY_LOSS_PCT x capital?
[ ] 2. trades_today < MAX_TRADES_PER_DAY?
[ ] 3. Signal RR >= MIN_REWARD_RISK (2.0)?
[ ] 4. Stop loss is ATR-based (not arbitrary)?
[ ] 5. Not within 5 min of market close (23:25)?
[ ] 6. Not on contract expiry day (unless confirmed)?
[ ] 7. Position size <= MAX_POSITION_PCT of capital?
[ ] 8. Margin available in account?
```

If ANY check fails -> output REJECT with specific reason.

## Daily Loss Limit Logic

```python
class DailyLimitTracker:
    def __init__(self, capital):
        self.capital   = capital
        self.daily_pnl = 0.0
        self.limit     = capital * 2.0 / 100  # 2% = ₹2,000 on ₹1L

    def can_trade(self):
        return self.daily_pnl > -self.limit

    def update(self, pnl):
        self.daily_pnl += pnl

    def status(self):
        used_pct = abs(self.daily_pnl) / self.limit * 100
        return f"Daily P&L: ₹{self.daily_pnl:.0f} | " \
               f"Limit used: {used_pct:.1f}%"

    def reset(self):  # Called at midnight
        self.daily_pnl = 0.0
```

## Risk Output Format

When approving or rejecting a trade:

```
✅ APPROVED / ❌ REJECTED
--------------------------
Symbol     : CRUDEOIL
Lots       : 2
Risk Amount: ₹1,000 (1.0% of ₹1,00,000)
SL Points  : 5.0 pts x 100 lots = ₹1,000
RR Ratio   : 2.4 : 1  ✅
Daily Used : ₹500 / ₹2,000 (25%) ✅
Trades Left: 4 / 5 ✅

Margin Est : ₹8,500 per lot x 2 = ₹17,000
```

## Trailing Stop Update Rules

Once in a position, update stops as follows:

| Gain from Entry | Action                              |
|-----------------|-------------------------------------|
| < +10%          | Hold original ATR stop              |
| +10% to +25%    | Move stop to breakeven (entry)      |
| > +25%          | Trail stop 10% below peak price     |
| TP1 hit (+25%)  | Sell 50% of position                |
| TP2 hit (+55%)  | Sell 30% more (hold 20% moonbag)    |
| TP3 hit (+100%) | Close remaining 20%                 |

## Risk Warnings to Always Flag

- 🚨 **Stop too wide**: SL > 3x ATR -> reject or resize
- ⚠️ **Expiry week**: Increased volatility, reduce size by 50%
- ⚠️ **High ADX (>40)**: Trend exhaustion possible, tighten TP
- 🚨 **Consecutive losses (3+)**: Suggest pause for the day
- ⚠️ **Low liquidity symbol**: ALUMINIUM/ZINC -> add slippage buffer
