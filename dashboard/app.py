"""Flask dashboard on port 5001 (the Solana bot owns 5000).

Read-only view over the SQLite DB — it renders what the bot did and why
(decision log included). It has NO control endpoints by design: control is
Telegram commands + host-side scripts, so a compromised dashboard port
cannot trade. Started as a daemon thread by scripts/run_bot.py, or
standalone:  venv\\Scripts\\python.exe -m dashboard.app
"""

import sqlite3

from flask import Flask, jsonify, render_template

from config import settings

app = Flask(__name__)


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
