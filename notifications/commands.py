"""Telegram admin commands — the remote kill switch.

Long-polls getUpdates on a daemon thread (Solana-bot pattern). Only the
configured TELEGRAM_CHAT_ID may command the bot. Destructive commands
(/halt) demand a CONFIRM reply within 60s.

    /status  — mode, equity, open positions, halt/pause flags
    /pause   — stop opening new positions (positions still managed)
    /resume  — clear the pause
    /halt    — close everything and HALT (CONFIRM required); cleared only
               by scripts/clear_halt.py on the host, never remotely
"""

import logging
import threading
import time

from config import settings
from database import models
from notifications.telegram import _API, _SESSION, send_message

logger = logging.getLogger(__name__)

_stop = threading.Event()
_pending_confirm: dict = {"cmd": None, "ts": 0.0}
CONFIRM_WINDOW_SECS = 60


def _get_updates(offset: int) -> list:
    try:
        url = _API.format(token=settings.TELEGRAM_BOT_TOKEN,
                          method="getUpdates")
        resp = _SESSION.post(url, json={"offset": offset, "timeout": 4},
                             timeout=10)
        data = resp.json()
        return data.get("result", []) if data.get("ok") else []
    except Exception as exc:
        logger.debug("getUpdates failed: %s", exc)
        return []


def _handle(text: str, engine) -> None:
    text = text.strip().lower()

    if _pending_confirm["cmd"] and \
            time.time() - _pending_confirm["ts"] < CONFIRM_WINDOW_SECS:
        if text == "confirm":
            cmd = _pending_confirm["cmd"]
            _pending_confirm["cmd"] = None
            if cmd == "/halt":
                models.set_halted(True, engine.db)
                for ev in engine.monitor.close_all("MANUAL_HALT"):
                    engine._on_close(ev)
                send_message("🚨 HALTED. All positions closed. Run "
                             "scripts/clear_halt.py on the host to resume.")
            return
        _pending_confirm["cmd"] = None  # anything else cancels

    if text == "/status":
        open_pos = models.get_open_positions(engine.db)
        lines = [f"{p['side']} {p['symbol']} x{p['qty']} @ "
                 f"₹{p['entry_price']:,.1f} (SL ₹{p['stop_loss']:,.1f})"
                 for p in open_pos] or ["none"]
        send_message(
            f"🤖 <b>STATUS</b> — "
            f"{'LIVE' if settings.LIVE_TRADING else 'PAPER'}\n"
            f"Equity: ₹{engine.equity:,.0f} "
            f"(realized ₹{engine.realized:,.0f})\n"
            f"Halted: {models.is_halted(engine.db)} | "
            f"Paused: {models.is_paused(engine.db)}\n"
            f"Open positions:\n" + "\n".join(lines))
    elif text == "/pause":
        models.set_state("paused", "1", engine.db)
        send_message("⏸ Paused — managing open positions, no new entries.")
    elif text == "/resume":
        models.set_state("paused", "0", engine.db)
        send_message("▶️ Resumed — new entries allowed again.")
    elif text == "/halt":
        _pending_confirm.update(cmd="/halt", ts=time.time())
        send_message("⚠️ /halt closes ALL positions and stops trading until "
                     "a manual host-side reset. Reply CONFIRM within 60s.")
    elif text == "/help":
        send_message("Commands: /status /pause /resume /halt")


def _poll_loop(engine) -> None:
    offset = 0
    while not _stop.is_set():
        for upd in _get_updates(offset):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            if chat_id != settings.TELEGRAM_CHAT_ID:
                continue  # only the owner commands the bot
            if msg.get("text"):
                try:
                    _handle(msg["text"], engine)
                except Exception as exc:
                    logger.error("command failed: %s", exc, exc_info=True)
        _stop.wait(3)


def start_command_poller(engine) -> threading.Thread | None:
    """No-op when Telegram is unconfigured (paper without a bot is fine)."""
    if not (settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID):
        logger.info("Telegram not configured — command poller disabled")
        return None
    t = threading.Thread(target=_poll_loop, args=(engine,), daemon=True,
                         name="tg-commands")
    t.start()
    logger.info("Telegram command poller started")
    return t
