"""Twice-daily hands-off briefings (mcx-daily-briefing skill).

Morning ~07:30 IST before the 09:00 open; evening ~23:45 after the close.
Both under 200 words, sent to Telegram. The evening report ALWAYS includes
the stop-loss audit — intended stop vs actual fill — because creeping stop
slippage was the prior bot's failure mode and this is its early-warning
siren. Scheduling: scripts/run_bot.py fires them once per day; cron works
too (see HANDOFF.md).
"""

from datetime import date, datetime, time as dtime, timedelta

from data.economic_calendar import EconomicCalendar
from database import models
from notifications.telegram import send_message


def _trim(text: str, max_words: int = 200) -> str:
    words = text.split()
    return text if len(words) <= max_words else " ".join(words[:max_words])


def _fmt_pnl(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}₹{abs(v):,.0f}"


def stop_loss_audit(db_path=None) -> str:
    """Intended vs filled on every stop exit today. DB has both numbers:
    stop_loss (intended, only ever tightened) and exit_price (fill)."""
    today = models.get_daily_pnl(db_path=db_path)["trades"]
    rows = [t for t in today if t["exit_reason"] == "STOP_LOSS"]
    if not rows:
        return "No stop-loss exits today ✅"
    # need full rows for stop_loss + exit price
    with models._conn(db_path) as c:
        full = [dict(r) for r in c.execute(
            """SELECT symbol, stop_loss, exit_price FROM trades
               WHERE status='CLOSED' AND exit_reason='STOP_LOSS'
               AND DATE(exit_time)=DATE('now')""")]
    lines = []
    for t in full:
        slip = abs(t["exit_price"] - t["stop_loss"]) / t["stop_loss"] * 100
        flag = "⚠️" if slip > 0.3 else "✅"
        lines.append(f"{flag} {t['symbol']}: stop ₹{t['stop_loss']:,.1f} → "
                     f"filled ₹{t['exit_price']:,.1f} ({slip:.2f}% slip)")
    return "\n".join(lines)


def market_context(today: date, calendar: EconomicCalendar) -> str:
    events = calendar.events_today(datetime.combine(today, dtime(9, 0)))
    src = calendar.source or "?"
    if not events:
        return f"No scheduled high-impact events today. [{src}]"
    listed = "; ".join(f"{e.name} ({'/'.join(e.symbols)}) "
                       f"{e.ts_ist():%H:%M} IST" for e in events)
    return f"Today [{src}]: {listed}"


def generate_morning_briefing(db_path=None,
                              expiry_days: dict[str, int] | None = None,
                              today: date | None = None,
                              calendar: EconomicCalendar | None = None) -> str:
    today = today or date.today()
    calendar = calendar or EconomicCalendar(db_path=db_path)
    open_pos = models.get_open_positions(db_path)
    yesterday = models.get_daily_pnl(
        (today - timedelta(days=1)).isoformat(), db_path)
    week = models.get_performance_summary(days=7, db_path=db_path)

    pos_lines = [f"• {p['side']} {p['symbol']} x{p['qty']} @ "
                 f"₹{p['entry_price']:,.1f} (SL ₹{p['stop_loss']:,.1f})"
                 for p in open_pos] or ["• none (flat overnight ✅)"]

    from positions.rollover import expiry_alerts
    risk_lines = expiry_alerts(expiry_days or {}) or ["• none"]

    pf = week["profit_factor"]
    text = f"""🌅 MCX MORNING BRIEFING — {today.isoformat()}

OPEN POSITIONS ({len(open_pos)})
{chr(10).join(pos_lines)}

YESTERDAY
Net P&L: {_fmt_pnl(yesterday['total'])} ({len(yesterday['trades'])} trades)

7-DAY STATS
Win rate {week['win_rate']:.0f}% | PF {'∞' if pf == float('inf') else f'{pf:.2f}'} | Net {_fmt_pnl(week['total_pnl'])}

MARKET CONTEXT
{market_context(today, calendar)}

RISK FLAGS
{chr(10).join(risk_lines)}"""
    return _trim(text)


def generate_evening_report(db_path=None, today: date | None = None) -> str:
    today = today or date.today()
    day = models.get_daily_pnl(db_path=db_path)
    trades = day["trades"]

    best = max(trades, key=lambda t: t["pnl"] or 0, default=None)
    worst = min(trades, key=lambda t: t["pnl"] or 0, default=None)

    def fmt(t):
        return (f"{t['symbol']} {t['side']} {_fmt_pnl(t['pnl'])} "
                f"({t['strategy']}, {t['exit_reason']})") if t else "—"

    flags = []
    losses = [t for t in trades if (t["pnl"] or 0) < 0]
    if len(losses) >= 3:
        flags.append(f"⚠️ {len(losses)} losing trades today")
    if not flags:
        flags.append("none")

    import json as _json
    try:
        st = _json.loads(models.get_state("scan_stats", "{}", db_path))
    except Exception:
        st = {}
    if st.get("date") == today.isoformat():
        machinery = (f"{st.get('scans', 0)} scans · "
                     f"{st.get('candidates', 0)} candidates · "
                     f"{st.get('approved', 0)} approved")
    else:
        machinery = "⚠️ NO SCANS RECORDED TODAY — check the engine!"

    text = f"""🌙 MCX EVENING REPORT — {today.isoformat()}

TODAY
Trades: {len(trades)} | Net P&L: {_fmt_pnl(day['total'])}
Machinery: {machinery}

BEST : {fmt(best)}
WORST: {fmt(worst)}

STOP-LOSS AUDIT
{stop_loss_audit(db_path)}

FLAGS
{chr(10).join(flags)}"""
    return _trim(text)


def send_morning_briefing(db_path=None, expiry_days=None,
                          calendar=None) -> bool:
    return send_message(generate_morning_briefing(db_path, expiry_days,
                                                  calendar=calendar))


def send_evening_report(db_path=None) -> bool:
    return send_message(generate_evening_report(db_path))
