"""Paper and live order execution behind ONE interface.

Strategy/risk/position code only ever sees OrderManager — it cannot tell
paper from live, which is what makes the paper->live switch a one-line
config change. Guardrails baked in:

- get_order_manager() returns PaperExecutor unless LIVE_TRADING=true AND
  validate_live_config() passes. There is no other way to obtain the live
  executor.
- LiveExecutor stamps the SEBI ALGO_ID_TAG on every order and refuses to
  build an order without it.
- Both executors support the SL-M (stop-loss market) resting backstop the
  hard-stop policy requires (mcx-portfolio-guard).

VERIFY BEFORE LIVE (step 2 research, smartapi docs): producttype for MCX
intraday futures is INTRADAY (CARRYFORWARD to hold overnight); ordertag is
the order-tag field. Confirm both against current Angel One docs when live
is actually being enabled — paper mode never sends them anywhere.
"""

import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from config import settings

logger = logging.getLogger(__name__)

LtpFn = Callable[[str], float]          # symbol -> last traded price
TokenFn = Callable[[str], str]          # symbol -> broker symboltoken


@dataclass
class Fill:
    order_id: str
    symbol: str
    side: str            # BUY / SELL
    qty: int             # lots
    price: float         # fill price (paper: LTP +/- slippage)
    status: str          # FILLED / PENDING / CANCELLED
    tag: str = ""
    ts: float = field(default_factory=time.time)


class OrderManager:
    """Interface both executors implement."""

    def place_market_order(self, symbol: str, side: str, qty: int,
                           tag: str = "") -> Fill:
        raise NotImplementedError

    def place_sl_market_order(self, symbol: str, side: str, qty: int,
                              trigger: float, tag: str = "") -> Fill:
        """Resting SL-M backstop at the 'exchange'."""
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError


class PaperExecutor(OrderManager):
    """Simulated fills against live (or mock) prices. Zero real orders.

    Market orders fill instantly at LTP plus adverse slippage
    (settings.PAPER_SLIPPAGE_PCT). SL-M orders rest in memory.

    NOTE: the engine deliberately does NOT call process_pending() — the
    position monitor fires stop exits itself and cancels the backstop.
    The resting SL-M only matters if the monitor dies, which cannot happen
    in-process in paper mode. Calling process_pending() from the engine
    loop would DOUBLE-CLOSE positions (see HANDOFF.md landmine L5).
    process_pending() exists for tests and for future multi-process designs.
    """

    def __init__(self, ltp_fn: LtpFn):
        self._ltp = ltp_fn
        self._ids = itertools.count(1)
        self.pending: dict[str, Fill] = {}     # resting SL-M orders
        self.fills: list[Fill] = []            # audit trail

    def _next_id(self) -> str:
        return f"PAPER-{next(self._ids)}"

    def _slip(self, price: float, side: str) -> float:
        """Slippage always hurts: BUY fills higher, SELL fills lower."""
        s = price * settings.PAPER_SLIPPAGE_PCT / 100
        return price + s if side == "BUY" else price - s

    def place_market_order(self, symbol, side, qty, tag="") -> Fill:
        ltp = self._ltp(symbol)
        fill = Fill(self._next_id(), symbol, side, qty,
                    self._slip(ltp, side), "FILLED", tag)
        self.fills.append(fill)
        logger.info("[PAPER] %s %s %d lot(s) @ %.2f (%s)",
                    side, symbol, qty, fill.price, tag or "-")
        return fill

    def place_sl_market_order(self, symbol, side, qty, trigger,
                              tag="") -> Fill:
        order = Fill(self._next_id(), symbol, side, qty, trigger,
                     "PENDING", tag)
        self.pending[order.order_id] = order
        logger.info("[PAPER] resting SL-M %s %s %d lot(s) trigger %.2f",
                    side, symbol, qty, trigger)
        return order

    def process_pending(self) -> list[Fill]:
        """Trigger resting SL-M orders against current LTP; returns fills.

        A SELL stop (protecting a long) triggers when LTP <= trigger;
        a BUY stop (covering a short) triggers when LTP >= trigger.
        """
        done: list[Fill] = []
        for oid, order in list(self.pending.items()):
            ltp = self._ltp(order.symbol)
            hit = (ltp <= order.price if order.side == "SELL"
                   else ltp >= order.price)
            if hit:
                del self.pending[oid]
                fill = Fill(oid, order.symbol, order.side, order.qty,
                            self._slip(ltp, order.side), "FILLED", order.tag)
                self.fills.append(fill)
                logger.info("[PAPER] SL-M triggered: %s %s @ %.2f "
                            "(intended %.2f)", order.side, order.symbol,
                            fill.price, order.price)
                done.append(fill)
        return done

    def cancel_order(self, order_id) -> bool:
        return self.pending.pop(order_id, None) is not None


class LiveExecutor(OrderManager):
    """Real orders via Angel One. Only constructible when live is fully
    configured — instantiation is the safety gate."""

    def __init__(self, token_fn: TokenFn, product: str = "INTRADAY"):
        if not settings.LIVE_TRADING:
            raise settings.ConfigError(
                "LiveExecutor refused: LIVE_TRADING is false")
        settings.validate_live_config(live=True)  # raises if anything unset
        self._token_fn = token_fn
        self._product = product

    def _build_params(self, symbol: str, side: str, qty: int,
                      ordertype: str, variety: str,
                      trigger: float | None = None) -> dict:
        if not settings.ALGO_ID_TAG:
            raise settings.ConfigError(
                "SEBI algo tag missing — live order refused")
        params = {
            "variety": variety,                    # NORMAL / STOPLOSS
            "tradingsymbol": symbol,
            "symboltoken": self._token_fn(symbol),
            "transactiontype": side,               # BUY / SELL
            "exchange": "MCX",
            "ordertype": ordertype,                # MARKET / STOPLOSS_MARKET
            "producttype": self._product,
            "duration": "DAY",
            "quantity": qty,
            "ordertag": settings.ALGO_ID_TAG,      # SEBI: on EVERY order
        }
        if trigger is not None:
            params["triggerprice"] = trigger
        return params

    def _place(self, params: dict) -> Fill:
        from broker.auto_login import get_api, with_auth_retry

        @with_auth_retry
        def _do():
            return get_api().placeOrder(params)

        order_id = _do()
        logger.info("[LIVE] order %s placed: %s", order_id, params)
        return Fill(str(order_id), params["tradingsymbol"],
                    params["transactiontype"], params["quantity"],
                    0.0, "PENDING", params["ordertag"])

    def place_market_order(self, symbol, side, qty, tag="") -> Fill:
        return self._place(self._build_params(
            symbol, side, qty, "MARKET", "NORMAL"))

    def place_sl_market_order(self, symbol, side, qty, trigger,
                              tag="") -> Fill:
        return self._place(self._build_params(
            symbol, side, qty, "STOPLOSS_MARKET", "STOPLOSS",
            trigger=trigger))

    def cancel_order(self, order_id) -> bool:
        from broker.auto_login import get_api
        get_api().cancelOrder(order_id, "NORMAL")
        return True


def get_order_manager(ltp_fn: LtpFn,
                      token_fn: TokenFn | None = None) -> OrderManager:
    """The paper->live gate. Paper unless live is enabled AND configured."""
    if settings.LIVE_TRADING:
        settings.validate_live_config(live=True)
        logger.warning("LIVE order manager active — real orders will be "
                       "placed, tagged %s", settings.ALGO_ID_TAG)
        return LiveExecutor(token_fn)
    return PaperExecutor(ltp_fn)
