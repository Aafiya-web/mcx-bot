"""Backtest CLI — the go/no-go gate before any live consideration.

    venv\\Scripts\\python.exe scripts\\backtest.py [days]

With Angel One credentials it fetches real MCX history (6+ months for the
gate); without credentials it falls back to synthetic MockFeed data, which
exercises the machinery but is NOT valid for the go/no-go decision — the
report says so loudly.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.engine import (compute_metrics, format_report, go_no_go,  # noqa: E402
                             run_backtest)
from config import settings                                    # noqa: E402
from config.symbols import ACTIVE_SYMBOLS, INSTRUMENTS, active_symbol  # noqa: E402
from strategies.router import get_strategy                     # noqa: E402


def _real_history(days: int):
    from broker.auto_login import get_api
    from data.historical import fetch_ohlcv
    from positions.rollover import get_active_contract
    api = get_api()
    out = {}
    for base in ACTIVE_SYMBOLS:
        contract = get_active_contract(api, active_symbol(base))
        out[base] = fetch_ohlcv(api, contract["token"], "FIFTEEN_MINUTE",
                                days)
    return out, True


def _synthetic_history(days: int):
    from data.feed import MockFeed
    bars = days * 34  # ~34 15-min bars per MCX session
    feed = MockFeed(symbols=[active_symbol(b) for b in ACTIVE_SYMBOLS],
                    n_bars=bars, seed=7)
    feed.cursor = bars - 1
    return {b: feed.get_candles(active_symbol(b), lookback=bars)
            for b in ACTIVE_SYMBOLS}, False


def main() -> int:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    have_creds = all([settings.ANGEL_API_KEY, settings.ANGEL_CLIENT_ID,
                      settings.ANGEL_PASSWORD, settings.ANGEL_TOTP_KEY])
    history, real = (_real_history(days) if have_creds
                     else _synthetic_history(days))
    if not real:
        print("⚠️  SYNTHETIC DATA (no Angel One credentials): this run "
              "validates the machinery only.\n    It is NOT a valid "
              "go/no-go input — rerun with real history for the gate.\n")

    any_go = False
    for base in ACTIVE_SYMBOLS:
        df = history[base]
        if df.empty:
            print(f"{base}: no data\n")
            continue
        symbol = active_symbol(base)
        strategy = get_strategy(INSTRUMENTS[base]["strategy"])
        trades = run_backtest(df, strategy, symbol)
        metrics = compute_metrics(trades, settings.PAPER_CAPITAL)
        print(format_report(symbol, INSTRUMENTS[base]["strategy"], metrics,
                            settings.PAPER_CAPITAL))
        print()
        any_go = any_go or (not metrics.get("error")
                            and go_no_go(metrics)["go"])

    if not real:
        print("Reminder: verdicts above are on synthetic data — "
              "meaningless for live eligibility.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
