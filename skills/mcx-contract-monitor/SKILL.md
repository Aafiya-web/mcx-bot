---
name: mcx-contract-monitor
description: >
  Monitors MCX futures contract expiries and handles automatic rollover to
  the next month contract. Use this skill when the user asks about contract
  expiry, rollover, which contract is active, when does crude expire, how to
  avoid expiry-day issues, or wants to know if the bot needs to switch
  contracts. Also triggers for "is this near expiry?", "should I roll over?",
  "what's the next contract?", or any question about futures contract dates.
---

# MCX Contract Monitor

Tracks active futures contracts, alerts on approaching expiry, and
executes rollover to the next contract automatically.

## MCX Expiry Rules by Commodity

```python
# MCX contract expiry rules (approximate — always verify on MCX website)
EXPIRY_RULES = {
    "CRUDEOIL": {
        "type"       : "last_thursday_minus_1",  # Day before last Thursday
        "description": "19th or 20th of month usually",
        "roll_days"  : 3,   # Roll 3 days before expiry
    },
    "GOLD": {
        "type"       : "5th_last_day",
        "description": "5th last business day of delivery month",
        "roll_days"  : 5,
    },
    "SILVER": {
        "type"       : "5th_last_day",
        "description": "5th last business day of delivery month",
        "roll_days"  : 5,
    },
    "NATURALGAS": {
        "type"       : "last_thursday",
        "description": "Last Thursday of the month",
        "roll_days"  : 3,
    },
    "COPPER": {
        "type"       : "last_wednesday",
        "description": "Last Wednesday of the month",
        "roll_days"  : 3,
    },
}
```

## Active Contract Selector

```python
from datetime import datetime, date
import pandas as pd

def get_active_contract(api, symbol_base):
    """
    Fetch instrument list and return nearest non-expired contract.
    Avoids expiry-day contract which may have low liquidity.
    """
    instruments = api.searchScrip("MCX", symbol_base)
    today       = date.today()

    valid = []
    for inst in instruments['data']:
        sym    = inst['tradingsymbol']
        expiry = parse_expiry(sym)  # Extract date from symbol name
        if expiry and expiry > today:
            days_to_expiry = (expiry - today).days
            valid.append({
                "symbol"          : sym,
                "token"           : inst['symboltoken'],
                "expiry"          : expiry,
                "days_to_expiry"  : days_to_expiry,
            })

    if not valid:
        raise Exception(f"No valid contracts found for {symbol_base}")

    # Return nearest contract (most liquid)
    return sorted(valid, key=lambda x: x['expiry'])[0]

def parse_expiry(tradingsymbol):
    """
    Angel One symbol format: CRUDEOIL24JANFUT -> expiry = Jan 2024
    """
    import re
    match = re.search(r'(\d{2})([A-Z]{3})FUT', tradingsymbol)
    if not match:
        return None
    year_str, month_str = match.groups()
    months = {
        "JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
        "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12
    }
    year  = 2000 + int(year_str)
    month = months.get(month_str)
    if not month: return None
    # Approximate expiry as last business day of the month
    return date(year, month, 25)  # Refine per EXPIRY_RULES
```

## Rollover Logic

```python
def check_and_rollover(symbol, current_contract, position_manager, order_manager):
    """
    If within roll_days of expiry:
    1. Close current contract position
    2. Open equivalent position in next contract
    """
    roll_days = EXPIRY_RULES.get(symbol, {}).get("roll_days", 3)
    days_left = current_contract['days_to_expiry']

    if days_left > roll_days:
        return False  # No rollover needed

    from notifications.telegram import send_message

    # Get open position
    position = position_manager.get_position(symbol)
    if not position:
        return False  # Nothing to roll

    # Get next contract
    next_contract = get_next_contract(symbol, current_contract)

    send_message(
        f"🔄 ROLLOVER ALERT\n"
        f"Symbol : {symbol}\n"
        f"Current: {current_contract['symbol']} ({days_left}d left)\n"
        f"Next   : {next_contract['symbol']}\n"
        f"Action : Closing current, opening next same size"
    )

    # Close current
    side_close = "SELL" if position['side'] == "BUY" else "BUY"
    order_manager.place_market_order(
        current_contract['symbol'], side_close, position['qty'],
        tag="ROLLOVER_CLOSE")

    # Open next
    order_manager.place_market_order(
        next_contract['symbol'], position['side'], position['qty'],
        tag="ROLLOVER_OPEN")

    return True
```

## Daily Expiry Check (add to scheduler)

```python
import schedule

def daily_expiry_check():
    for symbol in ACTIVE_SYMBOLS:
        contract = get_active_contract(api, symbol)
        days     = contract['days_to_expiry']

        if days <= 1:
            send_message(f"🚨 {symbol} expires TODAY — no new entries!")
        elif days <= 3:
            send_message(f"⚠️ {symbol} expires in {days} days — prepare rollover")
        elif days <= 7:
            send_message(f"📅 {symbol} expires in {days} days")

# Run every morning at 9:05 AM
schedule.every().day.at("09:05").do(daily_expiry_check)
```

## Expiry Day Rules

| Days to Expiry | Rule                                         |
|----------------|----------------------------------------------|
| 7 days         | Alert only, trade normally                   |
| 3 days         | Reduce position size by 50%, prepare rollover|
| 1 day          | No new entries in expiring contract          |
| Expiry day     | Close all positions by 11:00 PM              |

## Symbol Format Reference (Angel One)

```
CRUDEOIL24JANFUT  -> Crude Oil January 2024 Futures
GOLD24FEBFUT      -> Gold February 2024 Futures
SILVERM24MARFUT   -> Silver Mini March 2024 Futures
```

Always verify token using `api.searchScrip("MCX", base_symbol)` —
do not hardcode tokens as they change each contract cycle.
