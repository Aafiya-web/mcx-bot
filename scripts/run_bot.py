"""The production entrypoint (what systemd runs).

    venv\\Scripts\\python.exe scripts\\run_bot.py            # Windows dev
    /home/ubuntu/mcx-trading-bot/venv/bin/python scripts/run_bot.py  # VM

Wiring: preflight -> feed (LiveFeed with credentials, MockFeed without,
so a fresh checkout always runs) -> order manager via the paper/live gate
-> engine + dashboard thread + Telegram command poller -> real-time loop
with once-a-day morning/evening briefings.
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
from config.symbols import ACTIVE_SYMBOLS, active_symbol  # noqa: E402
from core.engine import Engine                            # noqa: E402
from database import models                               # noqa: E402
from notifications import briefings                       # noqa: E402
from notifications.commands import start_command_poller   # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(),
              logging.FileHandler(settings.LOG_DIR / "bot.log",
                                  encoding="utf-8")])
logger = logging.getLogger("run_bot")

MORNING_AT = dtime(7, 30)
EVENING_AT = dtime(23, 45)


def build_feed():
    have_creds = all([settings.ANGEL_API_KEY, settings.ANGEL_CLIENT_ID,
                      settings.ANGEL_PASSWORD, settings.ANGEL_TOTP_KEY])
    if have_creds:
        from broker.auto_login import get_api
        from data.feed import LiveFeed
        from positions.rollover import get_active_contract
        api = get_api()
        token_map = {}
        for base in ACTIVE_SYMBOLS:
            contract = get_active_contract(api, active_symbol(base))
            token_map[active_symbol(base)] = contract["token"]
            logger.info("%s -> %s (token %s, %dd to expiry)", base,
                        contract["symbol"], contract["token"],
                        contract["days_to_expiry"])
        return LiveFeed(token_map)

    if settings.LIVE_TRADING:
        raise settings.ConfigError(
            "LIVE_TRADING=true but Angel One credentials are missing")
    logger.warning("No Angel One credentials — using synthetic MockFeed. "
                   "Paper results on mock data prove the PIPELINE, not the "
                   "strategies.")
    from data.feed import MockFeed
    return MockFeed(symbols=[active_symbol(b) for b in ACTIVE_SYMBOLS])


def _briefing_loop(engine):
    """Fire the two daily briefings, once each per day."""
    while True:
        now = datetime.now()
        for key, at, fire in (
            ("last_morning", MORNING_AT,
             lambda: briefings.send_morning_briefing(engine.db)),
            ("last_evening", EVENING_AT,
             lambda: briefings.send_evening_report(engine.db)),
        ):
            if (now.time() >= at
                    and models.get_state(key, "", engine.db)
                    != now.date().isoformat()):
                try:
                    fire()
                    models.set_state(key, now.date().isoformat(), engine.db)
                    logger.info("Sent %s briefing", key)
                except Exception as exc:
                    logger.error("briefing failed: %s", exc)
        time.sleep(60)


def main() -> int:
    settings.validate_live_config()  # hard refusal if live + unconfigured
    models.init_db()

    feed = build_feed()
    om = get_order_manager(feed.get_ltp)
    engine = Engine(feed, om)

    from dashboard.app import start_dashboard_thread
    start_dashboard_thread()
    logger.info("Dashboard on http://%s:%s", settings.DASHBOARD_HOST,
                settings.DASHBOARD_PORT)
    start_command_poller(engine)
    threading.Thread(target=_briefing_loop, args=(engine,), daemon=True,
                     name="briefings").start()

    engine.run_live_loop(interval_secs=3.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
