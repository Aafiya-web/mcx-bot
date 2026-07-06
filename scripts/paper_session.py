"""Paper-mode end-to-end checkpoint: run the FULL system — feed, regime,
strategies, agents, risk gates, execution, stops, squareoff — against the
synthetic MockFeed, in seconds, with zero credentials and zero real orders.

    venv\\Scripts\\python.exe scripts\\paper_session.py [seed] [bars]
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker.order_manager import PaperExecutor          # noqa: E402
from config import settings                              # noqa: E402
from config.symbols import ACTIVE_SYMBOLS, active_symbol # noqa: E402
from core.engine import Engine                           # noqa: E402
from data.feed import MockFeed                           # noqa: E402
from database import models                              # noqa: E402

logging.basicConfig(level=logging.WARNING)


def main() -> int:
    if settings.LIVE_TRADING:
        print("Refusing: paper session script with LIVE_TRADING=true")
        return 1

    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 42
    bars = int(sys.argv[2]) if len(sys.argv) > 2 else 900

    db = settings.PROJECT_ROOT / "paper_session.db"
    db.unlink(missing_ok=True)  # each run is a fresh simulated history

    symbols = [active_symbol(b) for b in ACTIVE_SYMBOLS]
    feed = MockFeed(symbols=symbols, n_bars=bars, seed=seed)
    engine = Engine(feed, PaperExecutor(feed.get_ltp), db_path=db)

    print(f"Running mock paper session: {len(symbols)} instruments, "
          f"~{bars - MockFeed.WARMUP} bars, seed {seed} ...")
    summary = engine.run_mock_session()

    print("=" * 58)
    print("PAPER SESSION REPORT (synthetic data — NOT a backtest)")
    print("=" * 58)
    print(f"Ticks processed    : {summary['ticks']}")
    print(f"Candidate signals  : {summary['candidates']}")
    print(f"Approved by agents : {summary['approved']}")
    print(f"Positions closed   : {summary['closes']}")
    print(f"Trades (closed)    : {summary['total_trades']}")
    if summary["total_trades"]:
        print(f"Win rate           : {summary['win_rate']:.0f}%")
        pf = summary["profit_factor"]
        print(f"Profit factor      : "
              f"{'inf' if pf == float('inf') else f'{pf:.2f}'}")
        print(f"By exit            : {summary['by_exit']}")
    print(f"Realized P&L       : ₹{summary['realized']:,.0f}")
    print(f"Final equity       : ₹{summary['equity']:,.0f} "
          f"(start ₹{engine.capital:,.0f})")

    with models._conn(db) as c:
        n_dec = c.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0]
        open_n = c.execute("SELECT COUNT(*) FROM trades WHERE "
                           "status='OPEN'").fetchone()[0]
    print(f"Decision log rows  : {n_dec}")
    print(f"Open at session end: {open_n} (squareoff must leave 0)")
    print("-" * 58)

    ok = summary["ticks"] > 0 and open_n == 0
    print("PAPER SESSION OK — full pipeline exercised." if ok
          else "PAPER SESSION FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
