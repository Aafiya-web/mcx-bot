"""The autonomous trading engine — deterministic layer of the hybrid design.

Every tick (cheap, no LLM):
    monitor stops/targets/trailing -> equity + circuit breaker -> squareoff
    -> scan for candidate signals

Only when a strategy yields a candidate BUY/SELL does the (expensive)
agentic layer fire: Orchestrator.evaluate() runs the full analyst/debate/
risk/PM pipeline. Approved candidates execute through the ONE OrderManager
interface — paper and live are the same code path from here.

Kill switches honored every tick, in order of severity:
    bot_state['halted']  — circuit breaker / manual halt: no trading at all,
                           cleared only by scripts/clear_halt.py
    bot_state['paused']  — soft pause: manage open positions, no new entries
"""

import logging
from datetime import datetime

from config import settings
from config.symbols import ACTIVE_SYMBOLS, INSTRUMENTS, POINT_VALUES, \
    active_symbol
from core import scheduler
from data.economic_calendar import EconomicCalendar
from core.orchestrator import Orchestrator
from core.regime import classify_regime, mtf_regime, volatility_ok
from database import models
from notifications import telegram
from positions.monitor import PositionMonitor
from risk.portfolio_guard import PortfolioGuard
from risk.position_sizing import DailyLimitTracker
from strategies.router import get_strategy
from agents.base import DecisionContext

logger = logging.getLogger(__name__)


class Engine:
    def __init__(self, feed, order_manager, db_path=None,
                 capital: float | None = None,
                 symbols: list[str] | None = None,
                 calendar: EconomicCalendar | None = None,
                 expiry_fn=None):
        """expiry_fn: optional zero-arg callable returning
        {base_symbol: days_to_expiry} for the active contracts — refreshed
        once per day (landmine L8). None (mock/paper without broker) means
        no expiry awareness, matching the pre-fix behavior."""
        settings.validate_live_config()  # refuses live-unconfigured startup
        self.feed = feed
        self.om = order_manager
        self.db = db_path
        self.capital = capital if capital is not None else (
            settings.INITIAL_CAPITAL if settings.LIVE_TRADING
            else settings.PAPER_CAPITAL)
        self.realized = 0.0

        models.init_db(db_path)
        self.symbols = symbols if symbols is not None else list(ACTIVE_SYMBOLS)
        self.calendar = calendar or EconomicCalendar(db_path=db_path)
        self.expiry_fn = expiry_fn
        # persist=True: daily loss/trade counters survive restarts (L6)
        self.daily = DailyLimitTracker(self.capital, persist=True,
                                       db_path=db_path)
        self.guard = PortfolioGuard(db_path=db_path)
        self.monitor = PositionMonitor(feed, order_manager, db_path)
        self.orchestrator = Orchestrator(self.daily, self.guard, db_path)
        self.strategies = {
            base: get_strategy(INSTRUMENTS[base]["strategy"])
            for base in self.symbols
        }
        self.stats = {"ticks": 0, "candidates": 0, "approved": 0,
                      "closes": 0, "scans": 0}
        self._squared_off_date = None
        self._last_scan_bucket = None   # 15-min bucket of the last scan
        self._maintenance_date = None   # daily reset / expiry refresh
        self.expiry_days: dict[str, int] = {}

    # ------------------------------------------------------------- equity

    def unrealized(self) -> float:
        total = 0.0
        for pos in models.get_open_positions(self.db):
            ltp = self.feed.get_ltp(pos["symbol"])
            move = ltp - pos["entry_price"]
            if pos["side"] == "SELL":
                move = -move
            total += move * pos["qty"] * POINT_VALUES.get(pos["symbol"], 1)
        return total

    @property
    def equity(self) -> float:
        return self.capital + self.realized + self.unrealized()

    # --------------------------------------------------------------- tick

    def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now()
        self.stats["ticks"] += 1

        # 0. Once per (IST) day: reset daily limits (L6), refresh contract
        #    expiries (L8). Cheap flag check on every other tick.
        if self._maintenance_date != now.date():
            self._maintenance_date = now.date()
            self.daily.roll_date(now.date())
            if self.expiry_fn:
                try:
                    self.expiry_days = self.expiry_fn()
                except Exception as exc:
                    logger.error("expiry refresh failed: %s", exc)
                    telegram.send_error("expiry refresh", str(exc))

        # 1. Manage what's open: stops fire here, on time, every tick.
        for ev in self.monitor.check():
            self._on_close(ev)

        # 2. Equity -> circuit breaker.
        if self.guard.update(self.equity):
            if models.get_open_positions(self.db):
                for ev in self.monitor.close_all("CIRCUIT_BREAKER"):
                    self._on_close(ev)
            return  # halted: nothing else happens, today or any day

        # 3. Session end: flatten everything once per day.
        if scheduler.in_squareoff_zone(now):
            if (self._squared_off_date != now.date()
                    and models.get_open_positions(self.db)):
                self._squared_off_date = now.date()
                for ev in self.monitor.close_all("SQUAREOFF"):
                    self._on_close(ev)
            return

        # 4. New entries — but only on a NEW 15-min bar (landmine L1).
        # Candle-based signals cannot change mid-bar, and in live mode a
        # scan costs many HTTP calls; scanning every tick would delay the
        # stop checks above, which must stay fast. Ticks between bar
        # closes do only LTP-level work.
        if not scheduler.entries_allowed(now):
            return
        if models.is_paused(self.db):
            return
        bucket = (now.date(), now.hour, now.minute // 15)
        if bucket == self._last_scan_bucket:
            return
        self._last_scan_bucket = bucket
        self.stats["scans"] += 1
        self._scan_for_entries(now)

    def _on_close(self, ev) -> None:
        self.realized += ev.pnl
        self.daily.record_close(ev.pnl)
        self.stats["closes"] += 1
        telegram.send_message(
            f"{'✅' if ev.pnl >= 0 else '🔻'} <b>{ev.exit_reason}</b> "
            f"{ev.symbol} {ev.side} x{ev.qty}\n"
            f"P&L ₹{ev.pnl:,.0f}"
            + (f" | slip {ev.slippage_pct:.2f}%"
               if ev.exit_reason == "STOP_LOSS" else ""))

    # ------------------------------------------------------------ entries

    def _scan_for_entries(self, now: datetime) -> None:
        open_positions = models.get_open_positions(self.db)
        held = {p["symbol"] for p in open_positions}
        cal_today = self.calendar.events_today(now)

        for base in self.symbols:
            symbol = active_symbol(base)
            if symbol in held:
                continue  # one position per instrument

            df15 = self.feed.get_candles(symbol, "FIFTEEN_MINUTE", 200)
            df1h = self.feed.get_candles(symbol, "ONE_HOUR", 210)
            if len(df15) < 50:
                continue
            regime = classify_regime(df15)

            ok, _ = volatility_ok(base, regime.atr_pct)
            if not ok:
                continue

            signal = self.strategies[base].generate(df15, df1h, regime, now)
            if signal.action not in ("BUY", "SELL"):
                continue

            # Candidate found -> the agentic layer deliberates.
            self.stats["candidates"] += 1
            vol_ratio = (float(df15["volume"].iloc[-1])
                         / max(float(df15["volume"].iloc[-21:-1].mean()),
                               1e-9))
            ctx = DecisionContext(
                symbol=symbol, signal=signal, regime=regime,
                mtf=mtf_regime(self.feed, symbol),
                volume_ratio=round(vol_ratio, 2),
                open_positions=open_positions,
                capital=self.capital, equity=self.equity,
                days_to_expiry=self.expiry_days.get(base),
                consecutive_losses=self.daily.consecutive_losses,
                trades_today=self.daily.trades_today,
                weekday=now.weekday(),
                upcoming_events=self.calendar.upcoming_for(base, now),
                events_today=[e.name for e in cal_today
                              if base in e.symbols],
                calendar_source=self.calendar.source or "",
            )
            decision = self.orchestrator.evaluate(ctx)
            if not decision.approved:
                continue

            self.stats["approved"] += 1
            self.monitor.open_position(symbol, signal, decision.lots,
                                       mode="live" if settings.LIVE_TRADING
                                       else "paper")
            self.daily.record_entry()
            open_positions = models.get_open_positions(self.db)
            held = {p["symbol"] for p in open_positions}
            telegram.send_message(
                f"📈 <b>ENTRY</b> {signal.action} {symbol} "
                f"x{decision.lots} @ ~₹{signal.entry:,.1f}\n"
                f"stop ₹{signal.stop_loss:,.1f} | target "
                f"₹{signal.target:,.1f}\n{signal.strategy}: {signal.reason}")

    # ---------------------------------------------------------- sessions

    def run_mock_session(self, max_steps: int = 10_000) -> dict:
        """Drive the engine through a MockFeed until it is exhausted —
        the paper-mode end-to-end checkpoint."""
        while max_steps > 0 and self.feed.step():
            self.tick(self.feed.now)
            max_steps -= 1
        self.orchestrator.reflect()
        summary = models.get_performance_summary(days=3650, db_path=self.db)
        return {**self.stats, "equity": self.equity,
                "realized": self.realized, **summary}

    def run_live_loop(self, interval_secs: float = 3.0) -> None:
        """Real-time loop for the VM (paper OR live — same loop; the order
        manager decides what 'execute' means). Ctrl-C / SIGTERM to stop."""
        import time as _time
        logger.info("Engine loop starting (%s mode)",
                    "LIVE" if settings.LIVE_TRADING else "PAPER")
        telegram.send_message(
            f"🤖 MCX bot loop started "
            f"({'LIVE' if settings.LIVE_TRADING else 'PAPER'} mode)")
        try:
            while True:
                now = datetime.now()
                if scheduler.is_market_open(now) or \
                        scheduler.in_squareoff_zone(now):
                    try:
                        self.tick(now)
                    except Exception as exc:
                        logger.error("tick failed: %s", exc, exc_info=True)
                        telegram.send_error("engine tick", str(exc))
                _time.sleep(interval_secs)
        except KeyboardInterrupt:
            logger.info("Engine loop stopped by user")
