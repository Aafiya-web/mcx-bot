---
name: mcx-daily-briefing
description: >
  Generates the twice-daily hands-off briefings for the MCX bot — a pre-market
  morning briefing and a post-close evening performance report, sent to Telegram.
  Use this skill when the user asks about daily reports, morning/evening summaries,
  what the bot did today, market briefings, or making the bot hands-off. Also
  triggers for "send me a summary", "how did today go?", "what should I know
  before market open?", or setting up automated daily messages.
---

# MCX Daily Briefing

Turns the bot into a hands-off system. Instead of watching it all day, you get
two messages: one before MCX opens, one after it closes. Adapted from the
Claude Cowork morning/evening briefing pattern, tuned to MCX hours (IST).

## Timing (IST)

```
07:30 AM  →  Morning briefing (before 9:00 AM MCX open)
11:45 PM  →  Evening report (after 11:30 PM MCX close)
```

Schedule via Claude Cowork routines, cron, or the bot's own scheduler.

## Morning Briefing

```python
def generate_morning_briefing(db, market_data):
    """
    Pre-market briefing. Read positions + recent performance, add market context.
    Keep under 200 words. Send to Telegram.
    """
    open_positions = db.get_open_positions()
    yesterday_pnl  = db.get_daily_pnl(offset_days=1)
    week_stats     = db.get_performance_summary(days=7)

    briefing = f"""
🌅 MCX MORNING BRIEFING — {today_date()}

OPEN POSITIONS ({len(open_positions)})
{format_positions_with_unrealized(open_positions)}

YESTERDAY
Net P&L: ₹{yesterday_pnl['total']:.0f}
{format_per_symbol_pnl(yesterday_pnl)}

7-DAY STATS
Win rate: {week_stats['win_rate']:.0f}% | PF: {week_stats['profit_factor']:.2f}

MARKET CONTEXT
{market_context()}   # Crude: EIA today? Gold: Fed event? Volatility regime?

RISK FLAGS
{risk_flags(open_positions)}  # Near stop? Circuit breaker armed? Expiry soon?
"""
    return trim_to_words(briefing, 200)
```

### Market Context to Include

For MCX commodities specifically, the morning briefing should note:
- **CRUDEOIL**: Is there EIA inventory (Wed) or OPEC news today? Overnight WTI move?
- **GOLD/SILVER**: Any Fed decision, US CPI, or DXY move overnight?
- **NATURALGAS**: EIA gas storage (Thu)? Weather-driven demand shift?
- **Regime**: Is the overall tape trending or ranging right now?
- **Expiry**: Any contract within 3 days of expiry?

## Evening Report

```python
def generate_evening_report(db):
    """
    Post-close performance report. Read today's trades + P&L.
    Keep under 200 words. Send to Telegram.
    """
    today = db.get_daily_pnl(offset_days=0)
    trades = db.get_todays_trades()
    equity = db.get_current_equity()

    best  = max(trades, key=lambda t: t['pnl'], default=None)
    worst = min(trades, key=lambda t: t['pnl'], default=None)

    report = f"""
🌙 MCX EVENING REPORT — {today_date()}

TODAY
Trades: {len(trades)} | Net P&L: ₹{today['total']:.0f} ({today['pct']:+.1f}%)
Equity: ₹{equity:.0f}

BEST : {fmt_trade(best)}
WORST: {fmt_trade(worst)}

STOP-LOSS CHECK
{stop_loss_audit(trades)}   # Did stops fire at the right price? Any slippage?

VS BACKTEST
{backtest_divergence_check(db)}  # On track, or diverging from expected?

FLAGS
{evening_flags(db)}  # 3+ losses? Approaching daily limit? Circuit breaker?
"""
    return trim_to_words(report, 200)
```

### The Stop-Loss Audit (Critical)

Given the Solana bot's late-stop history, the evening report MUST explicitly
check whether stops fired correctly today:

```python
def stop_loss_audit(trades):
    stop_exits = [t for t in trades if t['exit_reason'] == 'STOP_LOSS']
    if not stop_exits:
        return "No stop-loss exits today ✅"

    lines = []
    for t in stop_exits:
        intended = t['stop_loss']
        actual   = t['exit_price']
        slippage = abs(actual - intended)
        slip_pct = slippage / intended * 100
        flag = "⚠️" if slip_pct > 0.3 else "✅"
        lines.append(
            f"{flag} {t['symbol']}: stop ₹{intended:.1f} → "
            f"filled ₹{actual:.1f} ({slip_pct:.2f}% slip)")
    return "\n".join(lines)
```

If stop slippage is consistently high, that's the early warning that the same
late-stop problem is creeping into the MCX bot — surface it every evening.

## Telegram Send

```python
def send_briefing(text):
    from notifications.telegram import send_message
    send_message(text)
```

## Setup as Cowork Routines

```
Morning routine (07:30 IST):
  "Read daily_pnl.csv and trades.csv from the MCX bot. Check current MCX
   positions via Angel One. Generate the morning briefing per the
   mcx-daily-briefing skill. Under 200 words. Send to Telegram."

Evening routine (23:45 IST):
  "Read today's entries in trades.csv and daily_pnl.csv. Generate the evening
   performance report per the mcx-daily-briefing skill, including the stop-loss
   audit. Under 200 words. Send to Telegram."
```

That's the entire daily involvement: read the morning message with chai, read
the evening message before bed. If something looks off, check the logs.
Otherwise the bot runs itself.
