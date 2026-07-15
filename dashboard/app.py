"""Flask dashboard on port 5001 (the Solana bot owns 5000).

Read-only view over the SQLite DB — it renders what the bot did and why
(decision log included). It has NO control endpoints by design: control is
Telegram commands + host-side scripts, so a compromised dashboard port
cannot trade. Started as a daemon thread by scripts/run_bot.py, or
standalone:  venv\\Scripts\\python.exe -m dashboard.app
"""

import hashlib
import hmac
import sqlite3

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request)

from config import settings

app = Flask(__name__)

_COOKIE = "mcxdash"
_COOKIE_AGE = 90 * 24 * 3600


def _cookie_token() -> str:
    """Cookie value derived from the password (never the password itself);
    changing DASHBOARD_PASSWORD invalidates every issued cookie."""
    return hashlib.sha256(
        ("mcx-dash:" + settings.DASHBOARD_PASSWORD).encode()).hexdigest()


@app.before_request
def _require_auth():
    """Three ways in, checked in order — all gated on DASHBOARD_PASSWORD
    (without a password the dashboard never leaves localhost):

    1. magic link  ?key=<password>  — sets a 90-day cookie and redirects
       to drop the key from the address bar (phone bookmark flow: mobile
       in-app browsers often can't show a Basic-Auth prompt at all);
    2. that cookie;
    3. HTTP Basic Auth (any username) for curl/scripts.
    """
    if not settings.DASHBOARD_PASSWORD:
        return None
    key = request.args.get("key")
    if key and hmac.compare_digest(key, settings.DASHBOARD_PASSWORD):
        resp = redirect(request.path or "/")
        resp.set_cookie(_COOKIE, _cookie_token(), max_age=_COOKIE_AGE,
                        httponly=True, samesite="Lax")
        return resp
    if hmac.compare_digest(request.cookies.get(_COOKIE, ""),
                           _cookie_token()):
        return None
    auth = request.authorization
    if auth and hmac.compare_digest(auth.password or "",
                                    settings.DASHBOARD_PASSWORD):
        return None
    return Response("Authentication required", 401,
                    {"WWW-Authenticate": 'Basic realm="mcx-bot"'})


def _rows(query: str, args: tuple = ()) -> list[dict]:
    c = sqlite3.connect(str(settings.DB_FILE))
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(query, args).fetchall()]
    finally:
        c.close()


def _one(query: str, args: tuple = (), default=None):
    rows = _rows(query, args)
    return rows[0] if rows else default


@app.route("/")
def index():
    return render_template("index.html",
                           mode="LIVE" if settings.LIVE_TRADING else "PAPER",
                           port=settings.DASHBOARD_PORT)


@app.route("/api/status")
def api_status():
    halted = (_one("SELECT value FROM bot_state WHERE key='halted'")
              or {}).get("value") == "1"
    paused = (_one("SELECT value FROM bot_state WHERE key='paused'")
              or {}).get("value") == "1"
    open_pos = _rows("SELECT * FROM trades WHERE status='OPEN'")
    today = _one("""SELECT COALESCE(SUM(pnl),0) AS pnl, COUNT(*) AS n
                    FROM trades WHERE status='CLOSED'
                    AND DATE(exit_time)=DATE('now')""")
    return jsonify({
        "mode": "LIVE" if settings.LIVE_TRADING else "PAPER",
        "halted": halted,
        "paused": paused,
        "open_positions": open_pos,
        "today_pnl": today["pnl"],
        "today_trades": today["n"],
    })


@app.route("/api/trades")
def api_trades():
    return jsonify(_rows(
        "SELECT * FROM trades ORDER BY id DESC LIMIT 100"))


@app.route("/api/performance")
def api_performance():
    from database.models import get_performance_summary
    summary = get_performance_summary(days=30)
    if summary["profit_factor"] == float("inf"):
        summary["profit_factor"] = None  # JSON has no Infinity
    return jsonify(summary)


@app.route("/api/decisions")
def api_decisions():
    return jsonify(_rows(
        "SELECT * FROM decision_log ORDER BY id DESC LIMIT 200"))


@app.route("/api/param_changes")
def api_param_changes():
    return jsonify(_rows("SELECT * FROM param_changes ORDER BY id DESC"))


@app.route("/api/candles")
def api_candles():
    """15-min closes per active symbol for the price charts.
    ?days=1|2|5 (chart range presets; ~58 bars per MCX session)."""
    from config.symbols import ACTIVE_SYMBOLS, active_symbol
    days = max(1, min(int(request.args.get("days", 1)), 5))
    series = {}
    for base in ACTIVE_SYMBOLS:
        sym = active_symbol(base)
        rows = _rows(
            "SELECT ts, close FROM candles WHERE symbol=? AND "
            "interval='FIFTEEN_MINUTE' ORDER BY ts DESC LIMIT ?",
            (sym, days * 58))
        series[base] = [[r["ts"], r["close"]] for r in reversed(rows)]
    return jsonify({"symbols": list(ACTIVE_SYMBOLS), "series": series})


def start_dashboard_thread():
    """Run the dashboard as a daemon thread beside the engine."""
    import logging
    import threading
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    t = threading.Thread(
        target=lambda: app.run(host=settings.DASHBOARD_HOST,
                               port=settings.DASHBOARD_PORT,
                               debug=False, use_reloader=False),
        daemon=True, name="dashboard")
    t.start()
    return t


if __name__ == "__main__":
    app.run(host=settings.DASHBOARD_HOST, port=settings.DASHBOARD_PORT,
            debug=True)
