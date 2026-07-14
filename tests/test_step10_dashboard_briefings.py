"""Step 10 tests: dashboard endpoints against a seeded DB; briefing content
rules (word cap, stop-loss audit always present)."""

import pytest

from config import settings
from database import models
from notifications import briefings


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """Point the whole app at a temp DB with one win + one stop-out today."""
    db = tmp_path / "dash.db"
    monkeypatch.setattr(settings, "DB_FILE", db)
    models.init_db(db)

    t1 = models.log_trade("CRUDEOIL", "BUY", 2, 6000.0, 5950.0, 6100.0,
                          "supertrend", db_path=db)
    models.close_trade(t1, 6100.0, "TAKE_PROFIT", db_path=db)
    t2 = models.log_trade("GOLD", "SELL", 1, 72000.0, 72200.0, 71600.0,
                          "ema_trend", db_path=db)
    models.close_trade(t2, 72210.0, "STOP_LOSS", db_path=db)  # slipped 10
    models.log_trade("SILVER", "BUY", 1, 90000.0, 89500.0, 91000.0,
                     "supertrend_or_meanrev", db_path=db)     # still open
    models.log_decision("candidate_signal", "CRUDEOIL", "pm", "APPROVE",
                        "test", db_path=db)
    return db


# --------------------------------------------------------------- dashboard


@pytest.fixture
def client(seeded_db):
    from dashboard.app import app
    app.config["TESTING"] = True
    return app.test_client()


def test_index_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"MCX Trading Bot" in resp.data
    assert b"PAPER" in resp.data          # never LIVE in tests


def test_dashboard_auth_when_password_set(client, monkeypatch):
    import base64

    from config import settings as s
    monkeypatch.setattr(s, "DASHBOARD_PASSWORD", "sekret")
    assert client.get("/").status_code == 401           # no credentials
    bad = {"Authorization": "Basic "
           + base64.b64encode(b"x:wrong").decode()}
    assert client.get("/", headers=bad).status_code == 401
    good = {"Authorization": "Basic "
            + base64.b64encode(b"anyuser:sekret").decode()}
    assert client.get("/", headers=good).status_code == 200


def test_public_host_refused_without_password(monkeypatch):
    """settings falls back to loopback if a public bind has no password."""
    import importlib

    monkeypatch.setenv("DASHBOARD_HOST", "0.0.0.0")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    from config import settings as s
    importlib.reload(s)
    try:
        assert s.DASHBOARD_HOST == "127.0.0.1"
    finally:
        monkeypatch.undo()
        importlib.reload(s)


def test_api_status(client):
    data = client.get("/api/status").get_json()
    assert data["mode"] == "PAPER"
    assert data["halted"] is False
    assert len(data["open_positions"]) == 1
    assert data["today_trades"] == 2
    # CRUDEOIL +50pts x2 x100 = +20,000; GOLD short stopped -210pts x100
    # = -21,000 -> net -1,000.
    assert data["today_pnl"] == pytest.approx(-1000.0)


def test_api_trades_and_decisions(client):
    trades = client.get("/api/trades").get_json()
    assert len(trades) == 3
    decisions = client.get("/api/decisions").get_json()
    assert decisions[0]["stage"] == "pm"


def test_api_performance_json_safe(client):
    perf = client.get("/api/performance").get_json()
    assert perf["total_trades"] == 2
    assert perf["win_rate"] == pytest.approx(50.0)


# --------------------------------------------------------------- briefings


def test_morning_briefing_content(seeded_db):
    text = briefings.generate_morning_briefing(
        seeded_db, expiry_days={"CRUDEOIL": 2})
    assert "MORNING BRIEFING" in text
    assert "SILVER" in text                      # open position listed
    assert "CRUDEOIL expires in 2d" in text      # expiry risk flag
    assert len(text.split()) <= 200


def test_evening_report_has_stop_audit(seeded_db):
    text = briefings.generate_evening_report(seeded_db)
    assert "EVENING REPORT" in text
    assert "STOP-LOSS AUDIT" in text
    assert "GOLD" in text and "slip" in text     # the slipped stop is audited
    assert "BEST" in text and "WORST" in text
    assert len(text.split()) <= 200


def test_stop_audit_clean_day(tmp_path):
    db = tmp_path / "clean.db"
    models.init_db(db)
    assert "✅" in briefings.stop_loss_audit(db)


def test_briefings_send_via_telegram(seeded_db, monkeypatch):
    sent = {}
    monkeypatch.setattr("notifications.briefings.send_message",
                        lambda text: (sent.__setitem__("text", text), True)[1])
    assert briefings.send_evening_report(seeded_db) is True
    assert "EVENING REPORT" in sent["text"]
