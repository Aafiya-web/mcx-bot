"""MCX session clock (IST). Pure functions of `now` so mock runs and tests
drive them with feed time.

MCX evening close moves between 23:30 (summer) and 23:55 (winter, US DST).
We square off at 23:15 regardless — deliberately EARLY: a few minutes of
missed movement is cheaper than an overnight position because a clock or
calendar assumption slipped. VERIFY the current close time each season.
"""

from datetime import datetime, time as dtime

MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(23, 30)
NO_ENTRY_AFTER = dtime(23, 0)     # skill: no fresh signals near the close
SQUAREOFF_AFTER = dtime(23, 15)   # flatten everything from here
RELOGIN_AT = dtime(8, 50)         # SEBI daily session refresh


def is_trading_day(now: datetime) -> bool:
    return now.weekday() < 5  # MCX trades Mon-Fri (exchange holidays are
    # handled operationally: no data -> no candles -> no signals)


def is_market_open(now: datetime) -> bool:
    return is_trading_day(now) and MARKET_OPEN <= now.time() < MARKET_CLOSE


def entries_allowed(now: datetime) -> bool:
    return (is_market_open(now)
            and now.time() < NO_ENTRY_AFTER
            and now.time() >= MARKET_OPEN)


def in_squareoff_zone(now: datetime) -> bool:
    return is_trading_day(now) and now.time() >= SQUAREOFF_AFTER
