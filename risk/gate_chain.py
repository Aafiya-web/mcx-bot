"""The mandatory risk gate chain — NOTHING reaches execution except through
run_gate_chain():

    signal -> regime OK -> event blackout -> ATR sizing -> correlation
           -> portfolio guard

Every check's verdict is recorded so the decision log (and you) can see
exactly why a trade was taken or refused.
"""

from dataclasses import dataclass, field

from config.symbols import base_of
from risk.correlation import cluster_exposure_ok, correlation_check
from risk.portfolio_guard import PortfolioGuard
from risk.position_sizing import DailyLimitTracker, position_size


@dataclass
class GateResult:
    approved: bool
    lots: int = 0
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def rejection_reason(self) -> str:
        return "; ".join(d for _, ok, d in self.checks if not ok)


def run_gate_chain(signal, symbol: str, regime, open_positions: list[dict],
                   capital: float, daily: DailyLimitTracker,
                   guard: PortfolioGuard, equity: float,
                   upcoming_events: list | None = None) -> GateResult:
    """signal: strategies.base.Signal (BUY/SELL). Returns the full audit.

    upcoming_events: EconEvent-shaped objects (name/ts_ist()/symbols)
    already filtered to the blackout window by the calendar service."""
    res = GateResult(approved=False)

    def check(name: str, ok: bool, detail: str) -> bool:
        res.checks.append((name, ok, detail))
        return ok

    if not check("signal", signal.action in ("BUY", "SELL"),
                 f"{signal.action}: {signal.reason}"):
        return res

    # 0. Halt flag — after a circuit-breaker trip nothing trades.
    if not check("halt_flag", not guard.halted,
                 "halted — manual reset required" if guard.halted
                 else "clear"):
        return res

    # 1. Regime (the strategy layer already gates, but the chain re-checks
    #    so no future code path can skip it).
    if not check("regime", regime.can_trade,
                 f"{regime.regime} (ADX {regime.adx})"):
        return res

    # 1b. Event blackout: no NEW entries while a high-impact event for this
    #     instrument is imminent (window set by EVENT_BLACKOUT_MINUTES).
    blocking = [e for e in (upcoming_events or [])
                if base_of(symbol) in e.symbols]
    if not check("event_blackout", not blocking,
                 (f"BLOCKED: {blocking[0].name} at "
                  f"{blocking[0].ts_ist():%H:%M} IST inside blackout window"
                  if blocking else "no imminent high-impact events")):
        return res

    # 2. Daily limits.
    ok, detail = daily.can_trade()
    if not check("daily_limits", ok, detail):
        return res

    # 3. ATR position sizing.
    lots, detail = position_size(capital, symbol, signal.entry,
                                 signal.stop_loss)
    if not check("position_size", lots > 0, detail):
        return res

    # 4. Correlation filter.
    ok, detail = correlation_check(symbol, signal.action, open_positions)
    if not check("correlation", ok, detail):
        return res
    ok, detail = cluster_exposure_ok(symbol, lots, signal.entry,
                                     open_positions, capital)
    if not check("cluster_exposure", ok, detail):
        return res

    # 5. Portfolio guard (drawdown breaker armed state).
    if not check("portfolio_guard", not guard.update(equity),
                 guard.status(equity)):
        return res

    res.approved = True
    res.lots = lots
    return res
