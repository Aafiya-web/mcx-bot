---
name: mcx-trade-logger
description: >
  Logs, retrieves, and analyzes MCX trade history from the SQLite database.
  Use this skill when the user asks about past trades, P&L summary, win rate,
  best/worst trades, daily performance, trade history, how the bot performed,
  which strategy works best, or wants a report. Also triggers for "show me
  today's trades", "what's my P&L?", "how many winners?", "which symbol made
  the most?", or any request to review, audit, or export trading data.
---

# MCX Trade Logger

Reads and writes trade data to the SQLite database. Generates P&L reports,
win rate analysis, and strategy performance breakdowns.

## Database Schema

```sql
-- Main trades table (matches database/models.py)
CREATE TABLE trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,        -- BUY / SELL
    qty          INTEGER NOT NULL,
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
    entry_time   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    exit_time    TIMESTAMP,
    exit_reason  TEXT                  -- STOP_LOSS / TAKE_PROFIT / SQUAREOFF
);

CREATE TABLE daily_summary (
    id          INTEGER PRIMARY KEY,
    date        TEXT NOT NULL,
    total_pnl   REAL,
    trades      INTEGER,
    winners     INTEGER,
    losers      INTEGER,
    capital_end REAL
);
```

## Key Queries

```python
import sqlite3, pandas as pd

DB = "mcx_bot.db"

def get_open_positions():
    with sqlite3.connect(DB) as conn:
        return pd.read_sql(
            "SELECT * FROM trades WHERE status='OPEN'", conn)

def get_daily_pnl(date=None):
    date = date or "date('now')"
    with sqlite3.connect(DB) as conn:
        return pd.read_sql(f"""
            SELECT symbol, strategy, side, pnl, exit_reason
            FROM trades
            WHERE DATE(exit_time) = {date} AND status='CLOSED'
        """, conn)

def get_performance_summary(days=30):
    with sqlite3.connect(DB) as conn:
        df = pd.read_sql(f"""
            SELECT * FROM trades
            WHERE status='CLOSED'
            AND exit_time >= datetime('now', '-{days} days')
        """, conn)
    total  = len(df)
    winners = df[df['pnl'] > 0]
    losers  = df[df['pnl'] <= 0]
    return {
        "total_trades" : total,
        "win_rate"     : len(winners) / total * 100 if total else 0,
        "total_pnl"    : df['pnl'].sum(),
        "avg_win"      : winners['pnl'].mean() if len(winners) else 0,
        "avg_loss"     : losers['pnl'].mean()  if len(losers) else 0,
        "profit_factor": abs(winners['pnl'].sum() / losers['pnl'].sum())
                         if losers['pnl'].sum() != 0 else float('inf'),
        "by_strategy"  : df.groupby('strategy')['pnl'].sum().to_dict(),
        "by_symbol"    : df.groupby('symbol')['pnl'].sum().to_dict(),
        "by_exit"      : df.groupby('exit_reason')['pnl'].sum().to_dict(),
    }
```

## P&L Report Format

When asked for performance, always output this structure:

```
📊 MCX BOT PERFORMANCE REPORT
Period: Last 30 days | DB: mcx_bot.db
--------------------------------------
Capital Start : ₹1,00,000
Capital End   : ₹1,04,250
Net P&L       : +₹4,250 (+4.25%)

Trades        : 47
Win Rate      : 53.2% (25W / 22L)
Profit Factor : 1.8
Avg Win       : ₹680
Avg Loss      : ₹410
Best Trade    : +₹1,840 (GOLD, Supertrend)
Worst Trade   : -₹920  (CRUDEOIL, ORB)

BY STRATEGY
  Supertrend : +₹3,100  (28 trades, 57% WR)
  ORB        : +₹1,150  (19 trades, 47% WR)

BY SYMBOL
  CRUDEOIL   : +₹2,400
  GOLD       : +₹1,620
  SILVER     : +₹230

BY EXIT REASON
  Take Profit : +₹7,200  (23 trades)
  Stop Loss   : -₹2,950  (24 trades)
  Squareoff   : -₹0      (0 trades)
--------------------------------------
⚠️ Notes: Win rate below 55% — review ORB conditions
```

## Log a New Trade

```python
def log_trade(symbol, side, qty, entry, sl, tp, strategy):
    with sqlite3.connect(DB) as conn:
        conn.execute("""
            INSERT INTO trades
            (symbol, side, qty, entry_price, stop_loss,
             take_profit, strategy, status)
            VALUES (?,?,?,?,?,?,?,'OPEN')
        """, (symbol, side, qty, entry, sl, tp, strategy))

def close_trade(trade_id, exit_price, exit_reason):
    with sqlite3.connect(DB) as conn:
        row = conn.execute(
            "SELECT entry_price, qty, side FROM trades WHERE id=?",
            (trade_id,)).fetchone()
        entry, qty, side = row
        pnl = (exit_price - entry) * qty
        if side == "SELL": pnl = -pnl
        conn.execute("""
            UPDATE trades SET status='CLOSED', exit_price=?,
            exit_time=CURRENT_TIMESTAMP, exit_reason=?, pnl=?
            WHERE id=?
        """, (exit_price, exit_reason, pnl, trade_id))
```

## Analysis Flags to Always Check

- ❌ Win rate < 45% → strategy needs review
- ❌ Profit factor < 1.2 → risk:reward is broken
- ❌ Avg loss > avg win → stop losses are too tight or targets too small
- ✅ Profit factor > 1.5 + Win rate > 50% → healthy bot
- ⚠️ > 3 consecutive losses → flag for user review
- ⚠️ Stop loss exits > 60% of all exits → entries are poor quality
