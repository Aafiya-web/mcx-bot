"""Portfolio guard (mcx-portfolio-guard skill): the account's last line of
defense. (1) Drawdown circuit breaker — equity falls the configured % from
its peak -> close everything, HALT, require MANUAL reset. (2) Hard-stop
discipline — a stop may only ever tighten.

The halt flag is persisted in the bot_state table, so a restart cannot
silently resume trading after a trip. Auto-resume is deliberately not
implemented anywhere.
"""

import logging

from config import settings
from database import models

logger = logging.getLogger(__name__)


class PortfolioGuard:
    def __init__(self, max_drawdown_pct: float | None = None,
                 db_path=None):
        """max_drawdown_pct None -> settings.MAX_DRAWDOWN_PCT (which is None
        until the user sets it: breaker disarmed, fine for paper — the
        validate_live_config gate refuses LIVE mode in that state)."""
        self.max_drawdown_pct = (max_drawdown_pct
                                 if max_drawdown_pct is not None
                                 else settings.MAX_DRAWDOWN_PCT)
        self._db_path = db_path
        # Landmine L7 fix: the peak persists like the halt flag, so a
        # restart cannot quietly re-arm the breaker from a lower equity.
        raw = models.get_state("equity_peak", "", db_path)
        self.equity_peak: float | None = float(raw) if raw else None

    @property
    def halted(self) -> bool:
        return models.is_halted(self._db_path)

    def drawdown_pct(self, equity: float) -> float:
        if not self.equity_peak:
            return 0.0
        return (self.equity_peak - equity) / self.equity_peak * 100

    def update(self, equity: float) -> bool:
        """Call on every equity change. Returns True if trading is halted."""
        if self.equity_peak is None or equity > self.equity_peak:
            self.equity_peak = equity
            models.set_state("equity_peak", str(equity), self._db_path)
        if self.halted:
            return True
        if self.max_drawdown_pct is None:
            return False  # breaker not armed (placeholder unset)
        if self.drawdown_pct(equity) >= self.max_drawdown_pct:
            self._trip(equity)
            return True
        return False

    def _trip(self, equity: float) -> None:
        from notifications.telegram import send_message
        models.set_halted(True, self._db_path)
        logger.critical("CIRCUIT BREAKER TRIPPED: %s", self.status(equity))
        send_message(
            "🚨🚨 <b>CIRCUIT BREAKER TRIPPED</b> 🚨🚨\n"
            f"{self.status(equity)}\n"
            "Closing ALL positions and HALTING trading.\n"
            "Manual review required before restart."
        )

    def close_all(self, order_manager, open_positions: list[dict]) -> None:
        """Trip sequence step 1: flatten everything with market orders."""
        for pos in open_positions:
            side = "SELL" if pos["side"] == "BUY" else "BUY"
            order_manager.place_market_order(pos["symbol"], side,
                                             pos["qty"],
                                             tag="CIRCUIT_BREAKER")

    def manual_reset(self) -> None:
        """The ONLY way trading resumes after a trip. Called by a human via
        scripts/clear_halt.py — never by the bot itself."""
        models.set_halted(False, self._db_path)
        self.equity_peak = None
        models.set_state("equity_peak", "", self._db_path)
        logger.warning("Halt flag manually cleared — trading may resume")

    def status(self, equity: float) -> str:
        armed = (f"{self.max_drawdown_pct}%" if self.max_drawdown_pct
                 else "DISARMED (max_drawdown_pct unset)")
        peak = f"₹{self.equity_peak:,.0f}" if self.equity_peak else "n/a"
        return (f"Peak: {peak} | Now: ₹{equity:,.0f} | "
                f"Drawdown: {self.drawdown_pct(equity):.1f}% / {armed}")


def validate_stop_change(position: dict,
                         proposed_new_sl: float) -> tuple[bool, str]:
    """A stop may only move in the profit-protecting direction. NEVER
    further from price, NEVER removed."""
    old_sl = position["stop_loss"]
    if position["side"] == "BUY":
        if proposed_new_sl < old_sl:
            return False, "REJECTED: cannot move long stop lower"
    else:
        if proposed_new_sl > old_sl:
            return False, "REJECTED: cannot move short stop higher"
    return True, "stop change allowed (tightening only)"
