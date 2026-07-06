---
name: mcx-portfolio-guard
description: >
  Portfolio-level safety: maximum drawdown circuit breaker, equity peak tracking,
  and hard-stop enforcement policy for the MCX bot. Use this skill when the user
  asks about circuit breakers, max drawdown limits, halting all trading, protecting
  the account from catastrophic loss, or the rules for how stops behave. Also
  triggers for "when does the bot stop everything?", "how do I prevent a blowup?",
  "why won't it move my stop?", or any account-level protection question.
---

# MCX Portfolio Guard

The account's last line of defense. Two jobs: (1) a portfolio drawdown circuit
breaker that halts everything after a catastrophic loss, and (2) enforcing the
hard-stop discipline that stops the bot from ever loosening a stop.

## 1. Drawdown Circuit Breaker

Tracks equity peak and halts all trading if the account falls too far from it.

```python
class PortfolioGuard:
    def __init__(self, max_drawdown_pct=None):
        """
        max_drawdown_pct: e.g. 10.0 means halt if equity drops 10% from peak.
        LEAVE AS None until the user sets it — the guard refuses LIVE mode
        while unset, but paper mode can run with a default for testing.
        """
        self.max_drawdown_pct = max_drawdown_pct
        self.equity_peak      = None
        self.halted           = False

    def update(self, current_equity):
        """Call on every equity change. Returns True if trading is halted."""
        if self.equity_peak is None or current_equity > self.equity_peak:
            self.equity_peak = current_equity

        if self.max_drawdown_pct is None:
            return False  # Not configured — no breaker in paper testing

        drawdown_pct = (self.equity_peak - current_equity) \
                       / self.equity_peak * 100

        if drawdown_pct >= self.max_drawdown_pct:
            self.halted = True
            return True
        return False

    def status(self, current_equity):
        if self.equity_peak is None:
            return "No peak recorded yet"
        dd = (self.equity_peak - current_equity) / self.equity_peak * 100
        return (f"Peak: ₹{self.equity_peak:.0f} | "
                f"Now: ₹{current_equity:.0f} | "
                f"Drawdown: {dd:.1f}% / {self.max_drawdown_pct or '—'}%")
```

## Circuit Breaker Trip Sequence

When the breaker trips, execute in this exact order:

```python
def trip_circuit_breaker(guard, order_manager, open_positions):
    from notifications.telegram import send_message

    send_message(
        "🚨🚨 CIRCUIT BREAKER TRIPPED 🚨🚨\n"
        f"{guard.status(current_equity)}\n"
        "Closing ALL positions and HALTING trading.\n"
        "Manual review required before restart."
    )

    # 1. Close every open position immediately (market orders)
    for pos in open_positions:
        side = "SELL" if pos['side'] == "BUY" else "BUY"
        order_manager.place_market_order(
            pos['symbol'], side, pos['qty'], tag="CIRCUIT_BREAKER")

    # 2. Set halt flag — engine must check this before any new entry
    guard.halted = True

    # 3. Require MANUAL reset — never auto-resume.
    #    The bot does not trade again until a human clears the halt.
```

**Critical:** after a trip, the bot must NOT resume on its own. A human reviews
what happened and manually clears the halt. Auto-resume defeats the purpose.

## 2. Hard-Stop Enforcement Policy

This directly addresses the #1 failure mode from the Solana bot — stops firing
late or being loosened. The policy has two halves: **discipline** (never loosen)
and **execution reliability** (fire on time).

### Discipline Rules (never violate)

```python
def validate_stop_change(position, proposed_new_sl):
    """
    A stop may only move in the profit-protecting direction.
    It may NEVER move further from price (never 'give it room'),
    and it may NEVER be removed.
    """
    old_sl = position['stop_loss']
    side   = position['side']

    if side == "BUY":
        # For longs, stop may only move UP (tighter/breakeven/trailing)
        if proposed_new_sl < old_sl:
            return False, "REJECTED: cannot move long stop lower"
    else:
        # For shorts, stop may only move DOWN
        if proposed_new_sl > old_sl:
            return False, "REJECTED: cannot move short stop higher"

    return True, "Stop change allowed (tightening only)"
```

### Execution Reliability (fire on time)

The Solana bot's stops fired *late* — worse prices than configured. Prevent that:

```
1. Pre-compute the exact stop price when the trade opens. Store it. Don't
   recalculate live (recalculation drift causes late fires).

2. Monitor loop checks LTP against the stored stop every few seconds.
   Do NOT depend on a slow indicator recompute to detect the breach.

3. On breach, fire a MARKET order immediately — never a limit order at the
   stop price (a limit may not fill in a fast move, which is exactly when
   you need the exit).

4. Where the broker supports it, ALSO place a real stop-loss order at the
   exchange as a backstop, so the exit fires even if the bot process is down.
   Angel One supports SL / SL-M order types — use SL-M (stop-loss market)
   for guaranteed exit on breach.

5. Log the intended stop price vs the actual fill price on every stop exit.
   Track the slippage. If average stop slippage grows, something in the
   monitor loop is lagging — investigate immediately.
```

```python
def place_hard_stop(order_manager, position):
    """
    Belt-and-suspenders: bot-monitored stop + a resting SL-M at the exchange.
    """
    # Resting stop-loss-market order at the broker as a backstop
    order_manager.place_sl_market_order(
        symbol   = position['symbol'],
        side     = "SELL" if position['side'] == "BUY" else "BUY",
        qty      = position['qty'],
        trigger  = position['stop_loss'],
        tag      = "HARD_STOP_BACKSTOP",
    )
```

## Guard Output Format

```
🛡️ PORTFOLIO GUARD STATUS
-------------------------------
Equity Peak    : ₹1,08,400
Current Equity : ₹1,02,100
Drawdown       : 5.8% / 10.0%  ✅
Circuit Breaker: ARMED (not tripped)
Open Positions : 2
Hard Stops     : 2/2 have exchange backstop ✅
Halt Flag      : CLEAR
-------------------------------
```

## Placeholder Discipline

Like capital and daily-loss limits, the `max_drawdown_pct` starts as `None`.
The guard runs harmlessly in paper mode, but the system must refuse to enter
LIVE mode until this is explicitly set. Document this in HANDOFF.md.
