"""The production entrypoint (what systemd runs).

    venv\\Scripts\\python.exe scripts\\run_bot.py            # Windows dev
    /home/ubuntu/mcx-trading-bot/venv/bin/python scripts/run_bot.py  # VM

Wiring: preflight -> feed (LiveFeed with credentials, MockFeed without,
so a fresh checkout always runs) -> order manager via the paper/live gate
-> engine + dashboard thread + Telegram command poller -> real-time loop
with once-a-day briefings and contract maintenance (expiry + rollover).

Contract months exist ONLY here (ContractBook) and at the execution
boundary; the engine, strategies, and DB all trade base names (see
HANDOFF.md landmine L8).
"""

import logging
import sys
import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker.order_manager import get_order_manager       # noqa: E402
from config import settings                               # noqa: E402
from config.symbols import ACTIVE_SYMBOLS, active_symbol, base_of  # noqa: E402
from core.engine import Engine                            # noqa: E402
from database import models                               # noqa: E402
from notifications import briefings                       # noqa: E402
from notifications.commands import start_command_poller   # noqa: E402
from notifications.telegram import send_error             # noqa: E402
from positions.rollover import (get_active_contract, get_next_contract,  # noqa: E402
                                needs_rollover, rollover_position)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(),
              logging.FileHandler(settings.LOG_DIR / "bot.log",
                                  encoding="utf-8")])
logger = logging.getLogger("run_bot")

MORNING_AT = dtime(7, 30)
EVENING_AT = dtime(23, 45)
RELOGIN_AT = dtime(8, 50)         # fresh session before MCX open (SEBI)
CONTRACT_MAINT_AT = dtime(9, 5)   # daily expiry check (contract skill)


def _have_creds() -> bool:
    return all([settings.ANGEL_API_KEY, settings.ANGEL_CLIENT_ID,
                settings.ANGEL_PASSWORD, settings.ANGEL_TOTP_KEY])


class ContractBook:
    """Active futures contract per traded symbol (live mode only)."""

    def __init__(self, api):
        self.api = api
        self.contracts: dict[str, dict] = {}   # traded symbol -> contract

    def refresh(self) -> None:
        for base in ACTIVE_SYMBOLS:
            symbol = active_symbol(base)
            c = get_active_contract(self.api, symbol)
            self.contracts[symbol] = c
            logger.info("%s -> %s (token %s, %dd to expiry)", symbol,
                        c["symbol"], c["token"], c["days_to_expiry"])
            time.sleep(1.0)   # searchScrip rate-limits back-to-back calls

    def set_contract(self, symbol: str, contract: dict) -> None:
        self.contracts[symbol] = contract

    def contract_fn(self, symbol: str) -> tuple[str, str]:
        """For LiveExecutor: base/traded name -> (tradingsymbol, token)."""
        c = self.contracts[symbol]
        return c["symbol"], c["token"]

    def token_map(self) -> dict[str, str]:
        return {sym: c["token"] for sym, c in self.contracts.items()}

    def expiry_days(self) -> dict[str, int]:
        """Keyed by BASE symbol, as the engine context expects."""
        return {base_of(sym): c["days_to_expiry"]
                for sym, c in self.contracts.items()}


def _contract_maintenance(engine, feed, book: ContractBook) -> None:
    """Once per day at 09:05: roll positions near expiry, re-resolve all
    contracts/tokens, refresh the engine's expiry map."""
    for pos in models.get_open_positions(engine.db):
        sym = pos["symbol"]
        contract = book.contracts.get(sym)
        if contract is None:
            continue
        if not needs_rollover(base_of(sym), contract["days_to_expiry"]):
            continue
        next_c = get_next_contract(book.api, sym)
        rollover_position(
            engine.monitor, base_of(sym), pos, next_c,
            # switch between close (old month) and reopen (new month):
            switch_fn=lambda s=sym, nc=next_c: (
                book.set_contract(s, nc),
                feed.update_token(s, nc["token"])))

    book.refresh()                     # picks up post-roll active months
    for sym, c in book.contracts.items():
        feed.update_token(sym, c["token"])
    engine.expiry_days = book.expiry_days()


def _broker_init_with_patience() -> ContractBook:
    """Login + contract resolution, surviving a broker outage.

    Angel One's API went down mid-session on 2026-07-14 ('not found' from
    the login endpoint itself); the old behavior crash-looped and hammered
    their login every few seconds. Now: retry every 2 minutes for up to an
    hour, telling the owner via Telegram (which doesn't depend on Angel),
    before letting systemd take over at its own slow cadence.
    """
    from broker.auto_login import get_api
    from notifications.telegram import send_message

    last_exc: Exception | None = None
    for attempt in range(30):
        try:
            book = ContractBook(get_api())
            book.refresh()
            if attempt > 0:
                send_message("✅ Broker is back — bot connected and "
                             "starting up.")
            return book
        except Exception as exc:
            last_exc = exc
            logger.error("broker init failed (attempt %d/30): %s",
                         attempt + 1, exc)
            if attempt == 0:
                send_message("🚨 Cannot reach Angel One "
                             f"({str(exc)[:120]}) — retrying every 2 min "
                             "for up to an hour. No trading until then.")
            time.sleep(120)
    raise RuntimeError(f"broker unreachable for an hour: {last_exc}")


def _daily_jobs_loop(engine, feed=None, book=None) -> None:
    """Fire each daily job once per day after its scheduled time. Job
    completion is recorded in bot_state so restarts don't re-fire."""
    def _morning():
        engine.calendar.refresh()      # fresh calendar for the new day
        return briefings.send_morning_briefing(
            engine.db, expiry_days=engine.expiry_days,
            calendar=engine.calendar)

    jobs = [("last_morning", MORNING_AT, _morning),
            ("last_evening", EVENING_AT,
             lambda: briefings.send_evening_report(engine.db))]
    if book is not None:
        # SEBI: Angel closes every session daily. Without this 08:50
        # re-login the Monday session death of 2026-07-13 repeats: the
        # boot-time JWT dies and the bot runs blind all day.
        from broker.auto_login import login as angel_login
        jobs.append(("last_relogin", RELOGIN_AT, angel_login))
        jobs.append(("last_contract_maint", CONTRACT_MAINT_AT,
                     lambda: _contract_maintenance(engine, feed, book)))

    retry_after: dict[str, float] = {}   # failed-job backoff (15 min)
    while True:
        now = datetime.now()
        for key, at, fire in jobs:
            if (now.time() >= at
                    and time.time() >= retry_after.get(key, 0.0)
                    and models.get_state(key, "", engine.db)
                    != now.date().isoformat()):
                try:
                    fire()
                    models.set_state(key, now.date().isoformat(), engine.db)
                    logger.info("Daily job done: %s", key)
                except Exception as exc:
                    retry_after[key] = time.time() + 900
                    logger.error("daily job %s failed (retry in 15 min): %s",
                                 key, exc, exc_info=True)
                    send_error(f"daily job {key}", str(exc))
        time.sleep(60)


def main() -> int:
    settings.validate_live_config()  # hard refusal if live + unconfigured
    models.init_db()

    # Dashboard FIRST: it reads only SQLite, and during a broker outage
    # the status page is exactly what the owner needs while the broker
    # init below waits (2026-07-14 outage: dashboard was unreachable for
    # the whole wait because it started last).
    from dashboard.app import start_dashboard_thread
    start_dashboard_thread()
    logger.info("Dashboard on http://%s:%s", settings.DASHBOARD_HOST,
                settings.DASHBOARD_PORT)

    book = None
    if _have_creds():
        from data.feed import LiveFeed
        book = _broker_init_with_patience()
        feed = LiveFeed(book.token_map())
        om = get_order_manager(
            feed.get_ltp, contract_fn=book.contract_fn,
            # Positional instruments order NRML so they CAN hold overnight;
            # the hold decision itself happens at session end in the engine.
            product_fn=lambda s: ("CARRYFORWARD"
                                  if base_of(s) in settings.POSITIONAL_SYMBOLS
                                  else "INTRADAY"))
        engine = Engine(feed, om, expiry_fn=book.expiry_days)
    else:
        if settings.LIVE_TRADING:
            raise settings.ConfigError(
                "LIVE_TRADING=true but Angel One credentials are missing")
        logger.warning("No Angel One credentials — using synthetic "
                       "MockFeed. Paper results on mock data prove the "
                       "PIPELINE, not the strategies.")
        from data.feed import MockFeed
        feed = MockFeed(symbols=[active_symbol(b) for b in ACTIVE_SYMBOLS])
        om = get_order_manager(feed.get_ltp)
        engine = Engine(feed, om)
    start_command_poller(engine)
    threading.Thread(target=_daily_jobs_loop, args=(engine, feed, book),
                     daemon=True, name="daily-jobs").start()

    engine.run_live_loop(interval_secs=3.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
