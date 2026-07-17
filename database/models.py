"""SQLite data layer — schema, trade logging, P&L queries, bot state.

Pattern reused from the Solana bot's state/logger.py: raw sqlite3, a tiny
per-operation connection factory, inline idempotent DDL, and a try/except
migration list for adding columns later. Additions over that pattern:
WAL journal mode (monitor loop + dashboard read concurrently) and an explicit
db_path parameter on every function so tests run against a temp database.

P&L is computed in rupees using config.symbols.POINT_VALUES:
    pnl = (exit - entry) * lots * point_value   (negated for shorts)
"""

import logging
import sqlite3
from pathlib import Path

from config import settings
from config.symbols import POINT_VALUES

logger = logging.getLogger(__name__)

_TRADES_DDL = """
CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,        -- BUY / SELL
    qty          INTEGER NOT NULL,     -- lots
    entry_price  REAL NOT NULL,
    exit_price   REAL,
    stop_loss    REAL NOT NULL,
    take_profit  REAL NOT NULL,
    peak_price   REAL,
    trailing_sl  REAL,
    tp1_hit      INTEGER DEFAULT 0,
    tp2_hit      INTEGER DEFAULT 0,
    pnl          REAL,
    status       TEXT DEFAULT 'OPEN',  -- OPEN / CLOSED
    strategy     TEXT,
    mode         TEXT DEFAULT 'paper', -- paper / live
    entry_time   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    exit_time    TIMESTAMP,
    exit_reason  TEXT                  -- STOP_LOSS / TAKE_PROFIT / SQUAREOFF / ...
)
"""

_DAILY_SUMMARY_DDL = """
CREATE TABLE IF NOT EXISTS daily_summary (
    id          INTEGER PRIMARY KEY,
    date        TEXT NOT NULL,
    total_pnl   REAL,
    trades      INTEGER,
    winners     INTEGER,
    losers      INTEGER,
    capital_end REAL
)
"""

_BOT_STATE_DDL = """
CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

# Append-only memory for the multi-agent layer (step 8). Created now so the
# schema is stable from day one.
_DECISION_LOG_DDL = """
CREATE TABLE IF NOT EXISTS decision_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    trigger   TEXT,                    -- candidate_signal / regime_shift / review / ...
    symbol    TEXT,
    stage     TEXT,                    -- analyst / debate / trader / risk / pm / reflection
    decision  TEXT,
    rationale TEXT,
    raw_json  TEXT
)
"""

# Versioned changelog of every parameter the Reflection agent adapts —
# what changed, when, why, and the old value so it can be rolled back.
_PARAM_CHANGES_DDL = """
CREATE TABLE IF NOT EXISTS param_changes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    param     TEXT NOT NULL,
    old_value REAL,
    new_value REAL,
    reason    TEXT
)
"""

# Weekly live-vs-replay divergence guard history (scripts/replay_check.py):
# per symbol per day, what the replay saw vs what the live bot logged.
_REPLAY_CHECKS_DDL = """
CREATE TABLE IF NOT EXISTS replay_checks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    period_start     TEXT NOT NULL,
    period_end       TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    day              TEXT NOT NULL,
    replay_candidates INTEGER NOT NULL,
    live_candidates  INTEGER NOT NULL,
    diverged         INTEGER NOT NULL DEFAULT 0
)
"""

# Idempotent column additions: (table, column, type). Each is attempted and
# an OperationalError (column exists) is ignored.
_MIGRATIONS: list[tuple[str, str, str]] = [
    # Risk distance at entry (rupee points). The live stop_loss tightens over
    # time, so R-based trailing needs the original distance preserved.
    ("trades", "initial_risk", "REAL"),
]


def _conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    c = sqlite3.connect(str(db_path or settings.DB_FILE))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db(db_path: Path | str | None = None) -> None:
    with _conn(db_path) as c:
        for ddl in (_TRADES_DDL, _DAILY_SUMMARY_DDL, _BOT_STATE_DDL,
                    _DECISION_LOG_DDL, _PARAM_CHANGES_DDL,
                    _REPLAY_CHECKS_DDL):
            c.execute(ddl)
        for table, column, coltype in _MIGRATIONS:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            except sqlite3.OperationalError:
                pass  # column already exists
    logger.info("Database initialized at %s", db_path or settings.DB_FILE)


# ------------------------------------------------------------------ trades


def log_trade(symbol: str, side: str, qty: int, entry: float, sl: float,
              tp: float, strategy: str, mode: str = "paper",
              db_path: Path | str | None = None) -> int:
    """Insert an OPEN trade; returns its row id."""
    with _conn(db_path) as c:
        cur = c.execute(
            """INSERT INTO trades
               (symbol, side, qty, entry_price, stop_loss, take_profit,
                strategy, mode, status, initial_risk, peak_price)
               VALUES (?,?,?,?,?,?,?,?,'OPEN',?,?)""",
            (symbol, side, qty, entry, sl, tp, strategy, mode,
             abs(entry - sl), entry),
        )
        return cur.lastrowid


def close_trade(trade_id: int, exit_price: float, exit_reason: str,
                db_path: Path | str | None = None) -> float:
    """Close a trade, compute rupee P&L via point value; returns the P&L."""
    with _conn(db_path) as c:
        row = c.execute(
            "SELECT symbol, entry_price, qty, side FROM trades WHERE id=?",
            (trade_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No trade with id {trade_id}")

        point_value = POINT_VALUES.get(row["symbol"], 1)
        pnl = (exit_price - row["entry_price"]) * row["qty"] * point_value
        if row["side"] == "SELL":
            pnl = -pnl

        c.execute(
            """UPDATE trades SET status='CLOSED', exit_price=?,
               exit_time=CURRENT_TIMESTAMP, exit_reason=?, pnl=?
               WHERE id=?""",
            (exit_price, exit_reason, pnl, trade_id),
        )
        return pnl


def get_open_positions(db_path: Path | str | None = None) -> list[dict]:
    with _conn(db_path) as c:
        rows = c.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()
    return [dict(r) for r in rows]


def update_stop(trade_id: int, new_sl: float,
                db_path: Path | str | None = None) -> None:
    """Persist a (validated) stop move. Direction is enforced by the
    portfolio guard BEFORE this is called — this function only stores."""
    with _conn(db_path) as c:
        c.execute("UPDATE trades SET trailing_sl=?, stop_loss=? WHERE id=?",
                  (new_sl, new_sl, trade_id))


def update_peak(trade_id: int, peak: float,
                db_path: Path | str | None = None) -> None:
    with _conn(db_path) as c:
        c.execute("UPDATE trades SET peak_price=? WHERE id=?",
                  (peak, trade_id))


def get_daily_pnl(date: str | None = None,
                  db_path: Path | str | None = None) -> dict:
    """Closed-trade P&L for a day (ISO date, default today)."""
    with _conn(db_path) as c:
        rows = c.execute(
            """SELECT symbol, strategy, side, pnl, exit_reason FROM trades
               WHERE DATE(exit_time) = COALESCE(?, DATE('now'))
               AND status='CLOSED'""",
            (date,),
        ).fetchall()
    trades = [dict(r) for r in rows]
    return {"total": sum(t["pnl"] or 0 for t in trades), "trades": trades}


def get_performance_summary(days: int = 30,
                            db_path: Path | str | None = None) -> dict:
    """Win rate / profit factor / breakdowns per the mcx-trade-logger skill."""
    with _conn(db_path) as c:
        rows = c.execute(
            f"""SELECT * FROM trades WHERE status='CLOSED'
                AND exit_time >= datetime('now', '-{int(days)} days')""",
        ).fetchall()
    trades = [dict(r) for r in rows]
    pnls = [t["pnl"] or 0 for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    def _group(key: str) -> dict:
        out: dict[str, float] = {}
        for t in trades:
            out[t[key]] = out.get(t[key], 0) + (t["pnl"] or 0)
        return out

    total = len(trades)
    return {
        "total_trades": total,
        "win_rate": len(wins) / total * 100 if total else 0.0,
        "total_pnl": sum(pnls),
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "profit_factor": (abs(sum(wins) / sum(losses))
                          if sum(losses) != 0 else float("inf")),
        "by_strategy": _group("strategy"),
        "by_symbol": _group("symbol"),
        "by_exit": _group("exit_reason"),
    }


# --------------------------------------------------------------- bot state


def set_state(key: str, value: str,
              db_path: Path | str | None = None) -> None:
    with _conn(db_path) as c:
        c.execute(
            """INSERT INTO bot_state (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, str(value)),
        )


def get_state(key: str, default: str = "",
              db_path: Path | str | None = None) -> str:
    try:
        with _conn(db_path) as c:
            row = c.execute(
                "SELECT value FROM bot_state WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default
    except sqlite3.Error:
        return default


def is_halted(db_path: Path | str | None = None) -> bool:
    """Circuit-breaker halt flag. Once set, only a manual reset clears it."""
    return get_state("halted", "0", db_path) == "1"


def set_halted(halted: bool, db_path: Path | str | None = None) -> None:
    set_state("halted", "1" if halted else "0", db_path)


def is_paused(db_path: Path | str | None = None) -> bool:
    """Soft pause (skip new entries); distinct from the hard halt."""
    return get_state("paused", "0", db_path) == "1"


# ------------------------------------------------------------ decision log


def log_decision(trigger: str, symbol: str, stage: str, decision: str,
                 rationale: str, raw_json: str = "",
                 db_path: Path | str | None = None) -> None:
    with _conn(db_path) as c:
        c.execute(
            """INSERT INTO decision_log
               (trigger, symbol, stage, decision, rationale, raw_json)
               VALUES (?,?,?,?,?,?)""",
            (trigger, symbol, stage, decision, rationale, raw_json),
        )


def log_param_change(param: str, old_value: float, new_value: float,
                     reason: str, db_path: Path | str | None = None) -> None:
    with _conn(db_path) as c:
        c.execute(
            """INSERT INTO param_changes (param, old_value, new_value, reason)
               VALUES (?,?,?,?)""",
            (param, old_value, new_value, reason),
        )


def get_param_history(param: str | None = None,
                      db_path: Path | str | None = None) -> list[dict]:
    q = "SELECT * FROM param_changes"
    args: tuple = ()
    if param:
        q += " WHERE param=?"
        args = (param,)
    with _conn(db_path) as c:
        return [dict(r) for r in c.execute(q + " ORDER BY id", args)]
