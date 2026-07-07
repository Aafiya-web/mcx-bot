"""Position monitor — the fire-on-time stop discipline (mcx-portfolio-guard).

Runs every few seconds from the engine. The rules that fix the prior bot's
late-stop failure mode:

1. The stop price is PRE-COMPUTED at entry and stored in the DB. The monitor
   compares LTP against the STORED number — no live recalculation drift.
2. On breach it fires a MARKET order immediately (never a limit).
3. A resting SL-M backstop also sits at the 'exchange' (real in live,
   simulated in paper) so the exit exists even if this process dies.
4. Every stop exit logs intended-vs-filled slippage. Watch it in the evening
   report — growing slippage means the loop is lagging.

Trailing ladder — DELIBERATE DEVIATION from the mcx-risk-manager skill: its
+10%/+25% price-move ladder came from the crypto bot; a 10% move in GOLD is
a once-a-year event, so that ladder would never trail. MCX uses R-multiples
(R = risk distance at entry): >= +1R -> stop to breakeven; >= +1.5R -> trail
1R behind the peak. Stops only ever tighten (validate_stop_change).
"""

import logging
from dataclasses import dataclass

from database import models
from risk.portfolio_guard import validate_stop_change

logger = logging.getLogger(__name__)

BREAKEVEN_AT_R = 1.0
TRAIL_AT_R = 1.5
TRAIL_DISTANCE_R = 1.0


@dataclass
class CloseEvent:
    trade_id: int
    symbol: str
    side: str
    qty: int
    exit_reason: str      # STOP_LOSS / TAKE_PROFIT / SQUAREOFF / ...
    intended_price: float
    fill_price: float
    pnl: float

    @property
    def slippage_pct(self) -> float:
        if not self.intended_price:
            return 0.0
        return abs(self.fill_price - self.intended_price) \
            / self.intended_price * 100


class PositionMonitor:
    def __init__(self, feed, order_manager, db_path=None):
        self.feed = feed
        self.om = order_manager
        self.db = db_path
        self._backstops: dict[int, str] = {}   # trade_id -> SL-M order id

    # ------------------------------------------------------------- entry

    def open_position(self, symbol: str, signal, lots: int,
                      mode: str = "paper") -> int:
        """Market entry + DB row + resting SL-M backstop. Returns trade id."""
        fill = self.om.place_market_order(symbol, signal.action, lots,
                                          tag=signal.strategy)
        trade_id = models.log_trade(symbol, signal.action, lots, fill.price,
                                    signal.stop_loss, signal.target,
                                    signal.strategy, mode, db_path=self.db)
        backstop_side = "SELL" if signal.action == "BUY" else "BUY"
        backstop = self.om.place_sl_market_order(
            symbol, backstop_side, lots, trigger=signal.stop_loss,
            tag="HARD_STOP_BACKSTOP")
        self._backstops[trade_id] = backstop.order_id
        logger.info("Opened #%d %s %s x%d @ %.2f, stop %.2f, target %.2f",
                    trade_id, signal.action, symbol, lots, fill.price,
                    signal.stop_loss, signal.target)
        return trade_id

    # -------------------------------------------------------------- exits

    def _close(self, pos: dict, reason: str,
               intended: float) -> CloseEvent:
        # Landmine L4: cancel the resting SL-M backstop FIRST. If the
        # cancel is rejected, the exchange already executed it — the
        # position is ALREADY flat, and firing our own market exit on top
        # would flip us into an unintended opposite position. In that case
        # the backstop's fill IS the exit.
        fill = None
        backstop = self._backstops.pop(pos["id"], None)
        if backstop and not self.om.cancel_order(backstop):
            fill = self.om.get_fill(backstop)
            if fill is not None:
                reason = "STOP_LOSS"          # the backstop is the stop
                intended = pos["stop_loss"]
                logger.warning("Backstop %s beat the monitor to the exit "
                               "for #%d — reconciled, no market order sent",
                               backstop, pos["id"])
        if fill is None:
            side = "SELL" if pos["side"] == "BUY" else "BUY"
            fill = self.om.place_market_order(pos["symbol"], side,
                                              pos["qty"], tag=reason)
        pnl = models.close_trade(pos["id"], fill.price, reason,
                                 db_path=self.db)
        ev = CloseEvent(pos["id"], pos["symbol"], pos["side"], pos["qty"],
                        reason, intended, fill.price, pnl)
        if reason == "STOP_LOSS":
            logger.info("STOP AUDIT #%d %s: intended %.2f filled %.2f "
                        "(%.3f%% slip)", pos["id"], pos["symbol"], intended,
                        fill.price, ev.slippage_pct)
        return ev

    def close_position(self, trade_id: int, reason: str) -> CloseEvent | None:
        """Explicit close (squareoff, rollover, circuit breaker)."""
        for pos in models.get_open_positions(self.db):
            if pos["id"] == trade_id:
                return self._close(pos, reason,
                                   self.feed.get_ltp(pos["symbol"]))
        return None

    def close_all(self, reason: str = "SQUAREOFF") -> list[CloseEvent]:
        return [self._close(pos, reason, self.feed.get_ltp(pos["symbol"]))
                for pos in models.get_open_positions(self.db)]

    # -------------------------------------------------------------- check

    def check(self) -> list[CloseEvent]:
        """One monitoring pass: stops, targets, trailing. Call every tick."""
        events: list[CloseEvent] = []
        for pos in models.get_open_positions(self.db):
            ltp = self.feed.get_ltp(pos["symbol"])
            is_long = pos["side"] == "BUY"

            # 1. Stop breach -> market out NOW.
            breached = (ltp <= pos["stop_loss"] if is_long
                        else ltp >= pos["stop_loss"])
            if breached:
                events.append(self._close(pos, "STOP_LOSS",
                                          pos["stop_loss"]))
                continue

            # 2. Target hit.
            hit = (ltp >= pos["take_profit"] if is_long
                   else ltp <= pos["take_profit"])
            if hit:
                events.append(self._close(pos, "TAKE_PROFIT",
                                          pos["take_profit"]))
                continue

            # 3. Peak tracking + R-based trailing (tighten only).
            peak = pos["peak_price"] or pos["entry_price"]
            peak = max(peak, ltp) if is_long else min(peak, ltp)
            if peak != pos["peak_price"]:
                models.update_peak(pos["id"], peak, self.db)

            self._maybe_trail(pos, ltp, peak)
        return events

    def _maybe_trail(self, pos: dict, ltp: float, peak: float) -> None:
        r = pos["initial_risk"] or 0
        if r <= 0:
            return
        is_long = pos["side"] == "BUY"
        gain_r = ((ltp - pos["entry_price"]) if is_long
                  else (pos["entry_price"] - ltp)) / r

        new_sl: float | None = None
        if gain_r >= TRAIL_AT_R:
            new_sl = (peak - TRAIL_DISTANCE_R * r if is_long
                      else peak + TRAIL_DISTANCE_R * r)
        elif gain_r >= BREAKEVEN_AT_R:
            new_sl = pos["entry_price"]
        if new_sl is None:
            return

        improved = (new_sl > pos["stop_loss"] if is_long
                    else new_sl < pos["stop_loss"])
        if not improved:
            return
        ok, why = validate_stop_change(pos, new_sl)
        if not ok:  # defense in depth — improved implies ok, but never trust
            logger.error("Trail rejected #%d: %s", pos["id"], why)
            return

        models.update_stop(pos["id"], new_sl, self.db)
        # keep the exchange backstop in sync with the tightened stop
        old = self._backstops.pop(pos["id"], None)
        if old:
            self.om.cancel_order(old)
        side = "SELL" if is_long else "BUY"
        backstop = self.om.place_sl_market_order(
            pos["symbol"], side, pos["qty"], trigger=new_sl,
            tag="HARD_STOP_BACKSTOP")
        self._backstops[pos["id"]] = backstop.order_id
        logger.info("Trailed #%d stop %.2f -> %.2f (gain %.1fR)",
                    pos["id"], pos["stop_loss"], new_sl, gain_r)
