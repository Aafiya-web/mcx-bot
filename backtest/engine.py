"""Backtest runner (mcx-backtest-runner skill) — the gate before live.

Walks bar-by-bar through history driving the SAME regime-gated strategy
objects and the SAME ATR sizing the live engine uses, so a backtest pass
actually says something about the deployed code. Costs are deliberately
conservative: ₹20 brokerage PER SIDE (₹40 round trip; the skill charged one
side only) plus slippage on both fills.

Go/no-go (master prompt, hard): PF > 1.5, max DD < 10%, win rate > 50%,
100+ trades. A strategy that fails does not go live — period.
"""

from dataclasses import dataclass

import pandas as pd

from config import settings
from config.symbols import POINT_VALUES
from core.regime import classify_regime
from risk.position_sizing import position_size

COMMISSION_PER_SIDE = 20.0     # ₹, Angel One flat
LOOKBACK = 250                 # bars of context per step (speed + realism)
WARMUP = 60


@dataclass
class BtTrade:
    symbol: str
    side: str
    lots: int
    entry: float
    exit: float
    exit_reason: str
    pnl: float
    entry_ts: object
    exit_ts: object


def _slip(price: float, side: str) -> float:
    s = price * settings.PAPER_SLIPPAGE_PCT / 100
    return price + s if side == "BUY" else price - s


def run_backtest(df15: pd.DataFrame, strategy, symbol: str,
                 capital: float | None = None) -> pd.DataFrame:
    """df15: full-history 15-min OHLCV. strategy: strategies.router object
    (already regime-gated). Returns a trades DataFrame."""
    capital = capital or settings.PAPER_CAPITAL
    point_value = POINT_VALUES[symbol]
    trades: list[BtTrade] = []
    position: dict | None = None

    for i in range(WARMUP, len(df15)):
        window = df15.iloc[max(0, i - LOOKBACK): i + 1]
        now = df15.index[i].to_pydatetime()
        bar = df15.iloc[i]

        if position is not None:
            is_long = position["side"] == "BUY"
            hit_sl = (bar["low"] <= position["sl"] if is_long
                      else bar["high"] >= position["sl"])
            hit_tp = (bar["high"] >= position["tp"] if is_long
                      else bar["low"] <= position["tp"])
            # same-bar ambiguity -> assume the stop hit first (conservative)
            exit_price, reason = None, None
            if hit_sl:
                exit_price, reason = position["sl"], "STOP_LOSS"
            elif hit_tp:
                exit_price, reason = position["tp"], "TAKE_PROFIT"
            elif i == len(df15) - 1 or \
                    df15.index[i + 1].date() != now.date():
                exit_price, reason = float(bar["close"]), "SQUAREOFF"

            if reason:
                exit_side = "SELL" if is_long else "BUY"
                fill = _slip(exit_price, exit_side)
                move = (fill - position["entry"]) if is_long \
                    else (position["entry"] - fill)
                pnl = move * position["lots"] * point_value \
                    - 2 * COMMISSION_PER_SIDE
                trades.append(BtTrade(symbol, position["side"],
                                      position["lots"], position["entry"],
                                      fill, reason, pnl,
                                      position["ts"], df15.index[i]))
                position = None
            continue

        # flat: look for an entry (same regime gate as live)
        h1 = (window.resample("1h")
              .agg({"open": "first", "high": "max", "low": "min",
                    "close": "last", "volume": "sum"}).dropna())
        regime = classify_regime(window)
        signal = strategy.generate(window, h1, regime, now)
        if signal.action not in ("BUY", "SELL"):
            continue

        lots, _ = position_size(capital, symbol, signal.entry,
                                signal.stop_loss)
        if lots < 1:
            continue
        position = {"side": signal.action, "lots": lots,
                    "entry": _slip(signal.entry, signal.action),
                    "sl": signal.stop_loss, "tp": signal.target,
                    "ts": df15.index[i]}

    return pd.DataFrame([t.__dict__ for t in trades])


def compute_metrics(trades: pd.DataFrame, capital: float) -> dict:
    if trades.empty:
        return {"error": "No trades generated", "total_trades": 0}
    pnl = trades["pnl"]
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]

    equity = capital + pnl.cumsum()
    drawdown = (equity - equity.cummax()) / equity.cummax() * 100
    ret = pnl / capital
    sharpe = (ret.mean() / ret.std() * (250 ** 0.5)
              if len(ret) > 1 and ret.std() != 0 else 0.0)

    return {
        "total_trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "profit_factor": (abs(wins.sum() / losses.sum())
                          if losses.sum() != 0 else float("inf")),
        "total_pnl": float(pnl.sum()),
        "return_pct": float(pnl.sum() / capital * 100),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "max_drawdown": float(drawdown.min()),
        "sharpe_ratio": float(sharpe),
        "expectancy": float(pnl.mean()),
        "by_exit": trades.groupby("exit_reason")["pnl"].sum().to_dict(),
    }


# The hard gate (master prompt). Every criterion must pass.
GO_CRITERIA = {
    "profit_factor": ("min", 1.5),
    "max_drawdown": ("dd_max", 10.0),   # drawdown is negative; |dd| < 10
    "win_rate": ("min", 50.0),
    "total_trades": ("min", 100),
}


def go_no_go(metrics: dict) -> dict:
    """Returns {'go': bool, 'criteria': [(name, ok, detail), ...]}."""
    checks = []
    if metrics.get("error"):
        return {"go": False, "criteria": [("trades", False,
                                           metrics["error"])]}
    for name, (kind, limit) in GO_CRITERIA.items():
        value = metrics[name]
        ok = (abs(value) < limit) if kind == "dd_max" else (value > limit)
        checks.append((name, ok, f"{value:.2f} vs "
                       f"{'<' if kind == 'dd_max' else '>'} {limit}"))
    return {"go": all(ok for _, ok, _ in checks), "criteria": checks}


def format_report(symbol: str, strategy_name: str, metrics: dict,
                  capital: float) -> str:
    if metrics.get("error"):
        return f"📊 BACKTEST {symbol}/{strategy_name}: {metrics['error']}"
    gate = go_no_go(metrics)
    pf = metrics["profit_factor"]
    lines = [
        f"📊 BACKTEST REPORT — {symbol} ({strategy_name})",
        "-" * 46,
        f"Capital   : ₹{capital:,.0f}",
        f"Net P&L   : ₹{metrics['total_pnl']:,.0f} "
        f"({metrics['return_pct']:+.1f}%)",
        f"Max DD    : {metrics['max_drawdown']:.1f}%",
        f"Sharpe    : {metrics['sharpe_ratio']:.2f}",
        f"Trades    : {metrics['total_trades']}",
        f"Win rate  : {metrics['win_rate']:.1f}%",
        f"PF        : {'inf' if pf == float('inf') else f'{pf:.2f}'}",
        f"Expectancy: ₹{metrics['expectancy']:,.0f}/trade",
        "-" * 46,
    ]
    for name, ok, detail in gate["criteria"]:
        lines.append(f"{'✅' if ok else '❌'} {name}: {detail}")
    lines.append("VERDICT: " + ("✅ GO — eligible for live consideration"
                                if gate["go"] else
                                "❌ NO-GO — must NOT trade live"))
    return "\n".join(lines)
