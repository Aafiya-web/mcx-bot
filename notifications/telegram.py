"""Best-effort Telegram notifications.

Pattern from the Solana bot's notifications/telegram.py — raw HTTP through a
shared requests.Session, self-disabling when the token is unset — with the
two gaps it had fixed here: retry with backoff on transient failures, and
respect for Telegram's 429 retry_after. Nothing in this module ever raises:
a notification failure must never take the trading loop down.

The admin command poller (/pause /halt /status) is added at step 9 when the
engine exists for it to control.
"""

import logging
import time

import requests

from config import settings

logger = logging.getLogger(__name__)

_SESSION = requests.Session()
_API = "https://api.telegram.org/bot{token}/{method}"
_MAX_ATTEMPTS = 3
_MAX_429_WAIT = 15  # seconds; longer server cooldowns are treated as failure


def enabled() -> bool:
    return bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)


_last_sent: dict[str, float] = {}   # dedupe_key -> monotonic timestamp


def send_message(text: str, parse_mode: str = "HTML",
                 dedupe_key: str | None = None,
                 cooldown_secs: float = 1800) -> bool:
    """Send a message; returns True on success. Silently no-ops when
    Telegram is unconfigured (paper mode without a bot token is fine).

    dedupe_key: identical keys within cooldown_secs send only once —
    a repeating failure (broker outage, crash-restart loop) must page the
    owner ONCE, not thirty times an hour (learned 2026-07-14 when an
    Angel outage flooded the owner's phone). Alert fatigue mutes bots.
    """
    if dedupe_key is not None:
        now = time.monotonic()
        last = _last_sent.get(dedupe_key)
        if last is not None and now - last < cooldown_secs:
            logger.debug("Telegram deduped (%s): %.60s", dedupe_key, text)
            return False
        _last_sent[dedupe_key] = now

    if not enabled():
        logger.debug("Telegram disabled — dropping message: %.80s", text)
        return False

    url = _API.format(token=settings.TELEGRAM_BOT_TOKEN, method="sendMessage")
    payload = {
        "chat_id": settings.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
    }

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = _SESSION.post(url, json=payload, timeout=8)
            data = resp.json()
            if data.get("ok"):
                return True

            if resp.status_code == 429:
                wait = data.get("parameters", {}).get("retry_after", 2)
                if wait > _MAX_429_WAIT:
                    logger.warning("Telegram 429, retry_after=%ss — giving up",
                                   wait)
                    return False
                time.sleep(wait)
                continue

            logger.warning("Telegram sendMessage not ok: %s",
                           data.get("description"))
            return False  # non-retryable API error (bad chat id, markup, ...)

        except Exception as exc:
            logger.warning("Telegram send attempt %d/%d failed: %s",
                           attempt, _MAX_ATTEMPTS, exc)
            if attempt < _MAX_ATTEMPTS:
                time.sleep(attempt)  # 1s, 2s backoff

    return False


def send_error(context: str, error: str) -> bool:
    """Error alert, deduped per context: the first occurrence pages the
    owner, repeats within 30 min only reach the log."""
    return send_message(
        f"🚨 <b>MCX BOT ERROR</b>\n{context}\n<code>{error}</code>",
        dedupe_key=f"err:{context}")
