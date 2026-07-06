"""SQLite candle cache — persists fetched OHLCV so backtests and restarts
don't refetch, and so the MockFeed can replay real recorded sessions."""

import sqlite3
from pathlib import Path

import pandas as pd

from config import settings

_CANDLES_DDL = """
CREATE TABLE IF NOT EXISTS candles (
    symbol   TEXT NOT NULL,
    interval TEXT NOT NULL,
    ts       TEXT NOT NULL,
    open     REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, interval, ts)
)
"""


def _conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    c = sqlite3.connect(str(db_path or settings.DB_FILE))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(_CANDLES_DDL)
    return c


def save_candles(symbol: str, interval: str, df: pd.DataFrame,
                 db_path: Path | str | None = None) -> int:
    """Upsert a timestamp-indexed OHLCV frame; returns rows written."""
    if df.empty:
        return 0
    rows = [
        (symbol, interval, ts.isoformat(),
         float(r["open"]), float(r["high"]), float(r["low"]),
         float(r["close"]), float(r["volume"]))
        for ts, r in df.iterrows()
    ]
    with _conn(db_path) as c:
        c.executemany(
            """INSERT INTO candles VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol, interval, ts) DO UPDATE SET
               open=excluded.open, high=excluded.high, low=excluded.low,
               close=excluded.close, volume=excluded.volume""",
            rows,
        )
    return len(rows)


def load_candles(symbol: str, interval: str, limit: int | None = None,
                 db_path: Path | str | None = None) -> pd.DataFrame:
    """Load cached candles (newest `limit`, chronological order)."""
    q = ("SELECT ts, open, high, low, close, volume FROM candles "
         "WHERE symbol=? AND interval=? ORDER BY ts DESC")
    params: tuple = (symbol, interval)
    if limit:
        q += " LIMIT ?"
        params += (limit,)
    with _conn(db_path) as c:
        df = pd.read_sql_query(q, c, params=params)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"])
    return df.set_index("ts").sort_index()
