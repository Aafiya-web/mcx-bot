---
name: mcx-backtest-runner
description: >
  Runs and interprets backtests for MCX trading strategies using historical
  OHLCV data. Use this skill when the user wants to test a strategy before
  going live, optimize parameters, compare strategies, see historical
  performance metrics, generate a backtest report, or asks "would this have
  worked?", "what are the best settings?", "how did Supertrend perform on
  crude?", or "should I use ORB or Supertrend?". Always backtest before
  enabling any new strategy live.
---

# MCX Backtest Runner

Tests strategies against historical MCX data before live deployment.
Uses vectorized pandas operations — no tick-by-tick loops.

## Data Source

```python
# Angel One historical data (free, up to 400 days)
from smartapi import SmartConnect

def fetch_historical(api, symbol_token, interval, from_date, to_date):
    """
    interval: ONE_MINUTE, THREE_MINUTE, FIVE_MINUTE,
              TEN_MINUTE, FIFTEEN_MINUTE, THIRTY_MINUTE,
              ONE_HOUR, ONE_DAY
    """
    params = {
        "exchange"   : "MCX",
        "symboltoken": symbol_token,
        "interval"   : interval,
        "fromdate"   : from_date,  # "YYYY-MM-DD HH:MM"
        "todate"     : to_date,
    }
    data = api.getCandleData(params)
    df = pd.DataFrame(data['data'],
         columns=['timestamp','open','high','low','close','volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.set_index('timestamp', inplace=True)
    return df
```

## Backtest Engine

```python
import pandas as pd
import pandas_ta as ta

def run_backtest(df, strategy_fn, get_sl_fn, get_tp_fn,
                 capital=100_000, risk_pct=1.0, commission=20):
    """
    strategy_fn : function(df_slice) -> "BUY" | "SELL" | "HOLD"
    get_sl_fn   : function(df_slice, signal) -> float
    get_tp_fn   : function(df_slice, signal, entry, sl) -> float
    commission  : ₹ per trade (Angel One = ₹20)
    """
    trades   = []
    position = None

    for i in range(50, len(df)):            # Warmup period
        slice_df = df.iloc[:i]
        signal   = strategy_fn(slice_df)

        if position is None and signal in ("BUY", "SELL"):
            entry = df['close'].iloc[i]
            sl    = get_sl_fn(slice_df, signal)
            tp    = get_tp_fn(slice_df, signal, entry, sl)

            if abs(tp - entry) / abs(entry - sl) < 2.0:
                continue  # Skip bad RR

            position = {
                "side" : signal, "entry": entry,
                "sl"   : sl,     "tp"   : tp,
                "entry_idx": i
            }

        elif position is not None:
            high  = df['high'].iloc[i]
            low   = df['low'].iloc[i]
            close = df['close'].iloc[i]

            hit_sl = (position['side'] == "BUY"  and low  <= position['sl']) or \
                     (position['side'] == "SELL" and high >= position['sl'])
            hit_tp = (position['side'] == "BUY"  and high >= position['tp']) or \
                     (position['side'] == "SELL" and low  <= position['tp'])

            exit_reason = None
            exit_price  = None

            if hit_sl:
                exit_price  = position['sl']
                exit_reason = "STOP_LOSS"
            elif hit_tp:
                exit_price  = position['tp']
                exit_reason = "TAKE_PROFIT"

            if exit_reason:
                pnl = (exit_price - position['entry'])
                if position['side'] == "SELL": pnl = -pnl
                pnl -= commission

                trades.append({
                    **position,
                    "exit_price" : exit_price,
                    "exit_reason": exit_reason,
                    "exit_idx"   : i,
                    "pnl"        : pnl,
                })
                position = None

    return pd.DataFrame(trades)
```

## Metrics Calculator

```python
def compute_metrics(trades_df, capital):
    if trades_df.empty:
        return {"error": "No trades generated"}

    pnl    = trades_df['pnl']
    wins   = pnl[pnl > 0]
    losses = pnl[pnl <= 0]

    # Equity curve
    equity = capital + pnl.cumsum()

    # Max drawdown
    rolling_max = equity.cummax()
    drawdown    = (equity - rolling_max) / rolling_max * 100
    max_dd      = drawdown.min()

    # Sharpe (annualized, assuming 250 trading days)
    daily_ret = pnl / capital
    sharpe    = (daily_ret.mean() / daily_ret.std()) * (250 ** 0.5) \
                if daily_ret.std() != 0 else 0

    return {
        "total_trades"  : len(trades_df),
        "win_rate"      : len(wins) / len(trades_df) * 100,
        "profit_factor" : abs(wins.sum() / losses.sum())
                          if losses.sum() != 0 else float('inf'),
        "total_pnl"     : pnl.sum(),
        "return_pct"    : pnl.sum() / capital * 100,
        "avg_win"       : wins.mean()   if len(wins)   else 0,
        "avg_loss"      : losses.mean() if len(losses) else 0,
        "max_drawdown"  : max_dd,
        "sharpe_ratio"  : sharpe,
        "expectancy"    : pnl.mean(),   # Per-trade expected value
        "by_exit"       : trades_df.groupby('exit_reason')['pnl'].sum().to_dict(),
    }
```

## Parameter Optimization

```python
def optimize_supertrend(df, periods=[7,10,14], multipliers=[2.0,2.5,3.0,3.5]):
    results = []
    for p in periods:
        for m in multipliers:
            from strategies.supertrend import SupertrendStrategy
            strat = SupertrendStrategy(period=p, multiplier=m)
            trades = run_backtest(df,
                lambda d: strat.generate_signal(d),
                lambda d, s: strat.get_stop_loss(d, s),
                lambda d, s, e, sl: strat.get_target(d, s, e, sl))
            if not trades.empty:
                metrics = compute_metrics(trades, 100_000)
                results.append({
                    "period": p, "mult": m, **metrics})
    return pd.DataFrame(results).sort_values("profit_factor", ascending=False)
```

## Backtest Report Format

```
📊 BACKTEST REPORT
Strategy  : Supertrend (10, 3.0)
Symbol    : CRUDEOIL
Timeframe : 15min
Period    : Jan 2024 - Dec 2024 (240 trading days)
---------------------------------------
Capital   : ₹1,00,000
Final     : ₹1,18,400  (+18.4%)
Max DD    : -6.2%
Sharpe    : 1.34

Trades    : 89
Win Rate  : 54.0% (48W / 41L)
PF        : 1.72
Avg Win   : ₹640
Avg Loss  : ₹380
Expectancy: ₹207 per trade

Exit Breakdown
  Take Profit : ₹30,800 (48 trades)
  Stop Loss   : -₹15,580 (41 trades)

⚠️ Worst stretch: -₹3,200 over 5 trades (Oct)
✅ Best month: +₹4,100 (March)
---------------------------------------
VERDICT: ✅ Deploy with live capital
(PF > 1.5, DD < 10%, Sharpe > 1.0)
```

## Go/No-Go Criteria

| Metric         | Minimum  | Good   | Excellent |
|----------------|----------|--------|-----------|
| Win Rate       | 45%      | 50%    | 55%+      |
| Profit Factor  | 1.3      | 1.5    | 2.0+      |
| Max Drawdown   | < 15%    | < 10%  | < 7%      |
| Sharpe Ratio   | > 0.8    | > 1.0  | > 1.5     |
| Total Trades   | > 50     | > 100  | > 200     |

❌ If any minimum is not met → do NOT deploy live.
