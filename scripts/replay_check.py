"""Weekly live-vs-replay divergence guard.

Motivating incident (2026-07-17, the forming-bar bug): the bot ran a full
week looking healthy — service up, data fresh, auth fine — while every
scan evaluated Angel's still-forming candle, making signals structurally
invisible. A replay of the SAME cached sessions found 22 signals where
the live decision log had zero. This job makes that comparison permanent:
it replays the past week's CACHED candles (never refetched — the point is
to compare against exactly the data the live bot saw) through the same
volatility gate + strategy objects the engine runs, and alerts when the
replay sees candidates the live bot never logged.

Run weekly by scripts/mcx-replay-check.timer (Sunday, market closed), or
manually:  venv/bin/python scripts/replay_check.py --days 7
"""

import argparse
import logging
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings                          # noqa: E402
from config.symbols import (ACTIVE_SYMBOLS, INSTRUMENTS,  # noqa: E402
                            active_symbol)
from core import scheduler                           # noqa: E402
from core.regime import classify_regime, volatility_ok  # noqa: E402
from data.store import load_candles                  # noqa: E402
from database import models                          # noqa: E402
from notifications.telegram import send_message      # noqa: E402
from strategies.router import get_strategy           # noqa: E402

logger = logging.getLogger(__name__)

LOOKBACK_15M = 250   # bars of context per replayed scan (mirrors live 200+)
LOOKBACK_1H = 210    # mirrors Engine._scan_symbol's df1h fetch


def replay_counts(start: date, end: date,
                  db_path=None) -> dict[str, dict[str, int]]:
    """{base: {iso_day: signal_count}} from cached candles only."""
    out: dict[str, dict[str, int]] = {}
    for base in ACTIVE_SYMBOLS:
        sym = active_symbol(base)
        df15 = load_candles(sym, "FIFTEEN_MINUTE", limit=3000,
                            db_path=db_path)
        df1h = load_candles(sym, "ONE_HOUR", limit=1500, db_path=db_path)
        days: dict[str, int] = defaultdict(int)
        out[base] = days
        if df15 is None or len(df15) < 60:
            continue
        strat = get_strategy(INSTRUMENTS[base]["strategy"])
        for i in range(60, len(df15)):
            ts = df15.index[i].to_pydatetime()
            naive = ts.replace(tzinfo=None)
            if not (start <= naive.date() <= end):
                continue
            if not scheduler.entries_allowed(naive):
                continue
            win15 = df15.iloc[max(0, i - LOOKBACK_15M): i + 1]
            regime = classify_regime(win15)
            ok, _ = volatility_ok(base, regime.atr_pct)
            if not ok:
                continue
            win1h = (df1h[df1h.index <= df15.index[i]].tail(LOOKBACK_1H)
                     if df1h is not None and len(df1h) else df1h)
            sig = strat.generate(win15, win1h, regime, naive)
            if sig.action in ("BUY", "SELL"):
                days[naive.date().isoformat()] += 1
    return out


def live_counts(start: date, end: date,
                db_path=None) -> dict[str, dict[str, int]]:
    """Candidates the live bot actually logged: one 'pm' decision row per
    candidate. Keyed by BASE symbol / iso day."""
    out: dict[str, dict[str, int]] = {b: defaultdict(int)
                                      for b in ACTIVE_SYMBOLS}
    with models._conn(db_path) as c:
        rows = c.execute(
            "SELECT symbol, DATE(ts) AS d, COUNT(*) AS n FROM decision_log "
            "WHERE stage='pm' AND DATE(ts) BETWEEN ? AND ? "
            "GROUP BY symbol, DATE(ts)",
            (start.isoformat(), end.isoformat())).fetchall()
    for sym, day, n in rows:
        for base in ACTIVE_SYMBOLS:
            if active_symbol(base) == sym or base == sym:
                out[base][day] += n
    return out


def compare(start: date, end: date, db_path=None) -> dict:
    """Replay vs live per base/day; persist history; return the verdict."""
    replay = replay_counts(start, end, db_path)
    live = live_counts(start, end, db_path)

    diverged: list[str] = []
    detail: list[str] = []
    with models._conn(db_path) as c:
        for base in ACTIVE_SYMBOLS:
            days = sorted(set(replay[base]) | set(live[base]))
            r_tot = sum(replay[base].values())
            l_tot = sum(live[base].values())
            detail.append(f"{base}: replay {r_tot} vs live {l_tot}")
            flag = 0
            for day in days:
                r, live_n = replay[base].get(day, 0), live[base].get(day, 0)
                if (r > 0 and live_n == 0) or (
                        live_n > 0 and r / live_n
                        > settings.REPLAY_ALERT_RATIO):
                    flag = 1
                    diverged.append(f"{base} {day}: replay {r}, live {live_n}")
                c.execute(
                    "INSERT INTO replay_checks (period_start, period_end, "
                    "symbol, day, replay_candidates, live_candidates, "
                    "diverged) VALUES (?,?,?,?,?,?,?)",
                    (start.isoformat(), end.isoformat(), base, day, r,
                     live_n, 1 if (r > 0 and live_n == 0) else 0))
            _ = flag

    result = {"start": start.isoformat(), "end": end.isoformat(),
              "diverged": diverged, "detail": detail}
    if diverged:
        send_message(
            "🧪 <b>LIVE/REPLAY DIVERGENCE</b> — possible silent pipeline "
            "bug (see HANDOFF: divergence guard).\n"
            + "\n".join(diverged[:12])
            + "\n\nTotals: " + " | ".join(detail),
            dedupe_key="replay-divergence")
    return result


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    models.init_db()
    end = date.today()
    start = end - timedelta(days=args.days)
    result = compare(start, end)
    print(f"Replay check {result['start']}..{result['end']}")
    for line in result["detail"]:
        print(" ", line)
    if result["diverged"]:
        print("DIVERGENCE (alert sent):")
        for line in result["diverged"]:
            print("  !", line)
    else:
        print("no divergence — live pipeline sees what the replay sees")
    return 0    # never fail the timer unit; the alert is the signal


if __name__ == "__main__":
    sys.exit(main())
