"""ATR-driven position sizing + daily limits (mcx-risk-manager skill).

The stop-loss distance determines the position size, never the other way
around: rupee risk stays constant across instruments regardless of their
volatility. One deliberate deviation from the skill snippet: when even ONE
lot exceeds the risk budget the trade is REFUSED (0 lots), not rounded up
to 1 — rounding up silently breaks the "risk% of capital" contract exactly
on the most volatile instruments, where it hurts most.
"""

import logging
from dataclasses import dataclass, field

from config import settings
from config.symbols import MARGIN_PCT_ESTIMATE, POINT_VALUES

logger = logging.getLogger(__name__)


def estimated_margin(symbol: str, price: float, lots: int = 1) -> float:
    """Rupee margin estimate for a position (see MARGIN_PCT_ESTIMATE)."""
    notional = lots * POINT_VALUES[symbol] * price
    return notional * MARGIN_PCT_ESTIMATE.get(symbol, 10.0) / 100


def position_size(capital: float, symbol: str, entry: float,
                  stop_loss: float,
                  risk_pct: float | None = None) -> tuple[int, str]:
    """Lots to trade for a constant rupee risk. Returns (lots, reason)."""
    risk_pct = (risk_pct if risk_pct is not None
                else settings.MAX_RISK_PER_TRADE_PCT)
    point_value = POINT_VALUES[symbol]
    sl_points = abs(entry - stop_loss)
    if sl_points <= 0:
        return 0, "invalid stop: zero distance"

    risk_amount = capital * risk_pct / 100
    risk_per_lot = sl_points * point_value
    lots = int(risk_amount / risk_per_lot)
    if lots < 1:
        return 0, (f"1 lot risks ₹{risk_per_lot:,.0f} > budget "
                   f"₹{risk_amount:,.0f} ({risk_pct}% of capital) — refused")

    # Margin-utilisation cap: futures are leveraged, so the concentration
    # cap is on estimated margin, not notional.
    margin_per_lot = estimated_margin(symbol, entry)
    max_margin = capital * settings.MAX_POSITION_MARGIN_PCT / 100
    capped = int(max_margin / margin_per_lot)
    if capped < 1:
        return 0, (f"1 lot needs ~₹{margin_per_lot:,.0f} margin, over the "
                   f"{settings.MAX_POSITION_MARGIN_PCT}% cap "
                   f"(₹{max_margin:,.0f}) — refused")
    if capped < lots:
        lots = capped

    return lots, (f"{lots} lot(s): risk ₹{lots * risk_per_lot:,.0f} of "
                  f"₹{risk_amount:,.0f} budget, margin "
                  f"~₹{lots * margin_per_lot:,.0f}")


@dataclass
class DailyLimitTracker:
    """Daily loss ceiling + overtrading counter.

    Landmine L6 fix: with persist=True the state survives restarts via the
    bot_state table (keyed by date), so a crash after losses cannot re-arm
    the full daily budget; roll_date() resets at IST day boundaries — the
    engine calls it every tick. Without persist (unit tests, ad-hoc use)
    it behaves as a plain in-memory counter.
    """

    capital: float
    max_loss_pct: float | None = None   # None (paper, unset) -> settings/2%
    daily_pnl: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0
    persist: bool = False
    db_path: object = None
    date: str = ""
    _default_pct: float = field(default=2.0, repr=False)

    _STATE_KEY = "daily_tracker"

    def __post_init__(self):
        if self.persist:
            self._load()

    def _load(self) -> None:
        import json
        from database import models
        raw = models.get_state(self._STATE_KEY, "", self.db_path)
        if not raw:
            return
        try:
            saved = json.loads(raw)
            self.date = saved["date"]
            self.daily_pnl = float(saved["daily_pnl"])
            self.trades_today = int(saved["trades_today"])
            self.consecutive_losses = int(saved["consecutive_losses"])
        except Exception as exc:
            logger.warning("daily tracker state unreadable (%s) — fresh "
                           "counters", exc)

    def _save(self) -> None:
        if not self.persist:
            return
        import json
        from database import models
        models.set_state(self._STATE_KEY, json.dumps({
            "date": self.date,
            "daily_pnl": self.daily_pnl,
            "trades_today": self.trades_today,
            "consecutive_losses": self.consecutive_losses,
        }), self.db_path)

    def roll_date(self, today) -> None:
        """Reset the counters when the (IST) date changes."""
        iso = today.isoformat()
        if self.date == iso:
            return
        if self.date:
            logger.info("Daily limits reset: %s -> %s (prev P&L ₹%.0f, "
                        "%d trades)", self.date, iso, self.daily_pnl,
                        self.trades_today)
        self.date = iso
        self.daily_pnl = 0.0
        self.trades_today = 0
        self.consecutive_losses = 0
        self._save()

    @property
    def limit(self) -> float:
        pct = (self.max_loss_pct if self.max_loss_pct is not None
               else (settings.MAX_DAILY_LOSS_PCT
                     if settings.MAX_DAILY_LOSS_PCT is not None
                     else self._default_pct))
        return self.capital * pct / 100

    def can_trade(self) -> tuple[bool, str]:
        if self.daily_pnl <= -self.limit:
            return False, (f"daily loss limit hit: ₹{self.daily_pnl:,.0f} "
                           f"<= -₹{self.limit:,.0f}")
        if self.trades_today >= settings.MAX_TRADES_PER_DAY:
            return False, (f"max {settings.MAX_TRADES_PER_DAY} trades/day "
                           f"reached")
        return True, (f"daily P&L ₹{self.daily_pnl:,.0f}, "
                      f"{self.trades_today} trades")

    def record_entry(self) -> None:
        self.trades_today += 1
        self._save()

    def record_close(self, pnl: float) -> None:
        self.daily_pnl += pnl
        self.consecutive_losses = (self.consecutive_losses + 1 if pnl < 0
                                   else 0)
        if self.consecutive_losses >= 3:
            logger.warning("3+ consecutive losses — consider pausing")
        self._save()

    def reset(self) -> None:
        self.daily_pnl = 0.0
        self.trades_today = 0
        self.consecutive_losses = 0
        self._save()
