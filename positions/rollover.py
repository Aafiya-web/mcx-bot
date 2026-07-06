"""Contract expiry tracking + rollover (mcx-contract-monitor skill).

Symbol tokens are never hardcoded — they change every contract cycle and
are resolved via api.searchScrip(). Expiry dates are parsed from the Angel
One trading symbol (CRUDEOIL26JULFUT); the conservative approximation and
per-commodity roll_days come from config.symbols.EXPIRY_RULES.
"""

import logging
import re
from datetime import date

from config.symbols import EXPIRY_RULES

logger = logging.getLogger(__name__)

_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def parse_expiry(tradingsymbol: str) -> date | None:
    """CRUDEOIL26JULFUT -> approx expiry date (conservative: the 20th, so we
    always roll early rather than late; exact dates come from the instrument
    master in live mode)."""
    m = re.search(r"(\d{2})([A-Z]{3})FUT$", tradingsymbol)
    if not m:
        return None
    year, mon = 2000 + int(m.group(1)), _MONTHS.get(m.group(2))
    if not mon:
        return None
    return date(year, mon, 20)


def days_to_expiry(tradingsymbol: str, today: date | None = None) -> int | None:
    expiry = parse_expiry(tradingsymbol)
    if expiry is None:
        return None
    return (expiry - (today or date.today())).days


def get_active_contract(api, base_symbol: str,
                        today: date | None = None) -> dict:
    """Nearest non-expired contract via searchScrip (live only)."""
    today = today or date.today()
    result = api.searchScrip("MCX", base_symbol)
    valid = []
    for inst in result.get("data", []):
        sym = inst["tradingsymbol"]
        expiry = parse_expiry(sym)
        if expiry and expiry > today:
            valid.append({"symbol": sym, "token": inst["symboltoken"],
                          "expiry": expiry,
                          "days_to_expiry": (expiry - today).days})
    if not valid:
        raise LookupError(f"No valid contracts found for {base_symbol}")
    return sorted(valid, key=lambda x: x["expiry"])[0]


def needs_rollover(base_symbol: str, contract_days_left: int) -> bool:
    roll_days = EXPIRY_RULES.get(base_symbol, {}).get("roll_days", 3)
    return contract_days_left <= roll_days


def rollover_position(monitor, base_symbol: str, position: dict,
                      next_contract: dict) -> int | None:
    """Close the expiring position and reopen the same exposure in the next
    contract. Returns the new trade id (None when nothing to roll)."""
    from notifications.telegram import send_message
    from strategies.base import Signal

    if not position:
        return None

    send_message(
        "🔄 <b>ROLLOVER</b>\n"
        f"Symbol : {base_symbol}\n"
        f"Closing: {position['symbol']} ({position['qty']} lot(s))\n"
        f"Opening: {next_contract['symbol']}"
    )
    monitor.close_position(position["id"], "ROLLOVER")

    # Same direction/size/levels, new contract symbol.
    sig = Signal(position["side"], position["strategy"] or "rollover",
                 entry=position["entry_price"],
                 stop_loss=position["stop_loss"],
                 target=position["take_profit"],
                 reason=f"rolled from {position['symbol']}")
    new_id = monitor.open_position(next_contract["symbol"], sig,
                                   position["qty"], mode=position["mode"])
    logger.info("Rolled %s: trade #%d -> #%d", base_symbol,
                position["id"], new_id)
    return new_id


def expiry_alerts(contracts: dict[str, int]) -> list[str]:
    """contracts: base symbol -> days to expiry. Returns alert lines for the
    daily briefing (7/3/1-day ladder from the skill)."""
    alerts = []
    for symbol, days in sorted(contracts.items()):
        if days <= 0:
            alerts.append(f"🚨 {symbol} expires TODAY — no new entries!")
        elif days <= 3:
            alerts.append(f"⚠️ {symbol} expires in {days}d — prepare rollover")
        elif days <= 7:
            alerts.append(f"📅 {symbol} expires in {days}d")
    return alerts
