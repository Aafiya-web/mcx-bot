"""Step 1 (Foundation) tests: config guard, symbols, database, telegram."""

import sqlite3

import pytest

from config import settings, symbols
from database import models


# ------------------------------------------------------------ config gate


def test_paper_mode_needs_no_config():
    # Everything unset is the shipped default — paper must pass silently.
    settings.validate_live_config(live=False)


def test_live_mode_refused_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "INITIAL_CAPITAL", None)
    monkeypatch.setattr(settings, "MAX_DAILY_LOSS_PCT", None)
    monkeypatch.setattr(settings, "MAX_DRAWDOWN_PCT", None)
    monkeypatch.setattr(settings, "ANGEL_API_KEY", "")
    monkeypatch.setattr(settings, "ALGO_ID_TAG", "")
    with pytest.raises(settings.ConfigError) as exc:
        settings.validate_live_config(live=True)
    msg = str(exc.value)
    for name in ("INITIAL_CAPITAL", "MAX_DAILY_LOSS_PCT", "MAX_DRAWDOWN_PCT",
                 "ANGEL_API_KEY", "ALGO_ID_TAG"):
        assert name in msg


def test_live_mode_passes_when_fully_configured(monkeypatch):
    monkeypatch.setattr(settings, "INITIAL_CAPITAL", 100000.0)
    monkeypatch.setattr(settings, "MAX_DAILY_LOSS_PCT", 2.0)
    monkeypatch.setattr(settings, "MAX_DRAWDOWN_PCT", 10.0)
    monkeypatch.setattr(settings, "ANGEL_API_KEY", "k")
    monkeypatch.setattr(settings, "ANGEL_CLIENT_ID", "c")
    monkeypatch.setattr(settings, "ANGEL_PASSWORD", "p")
    monkeypatch.setattr(settings, "ANGEL_TOTP_KEY", "t")
    monkeypatch.setattr(settings, "ALGO_ID_TAG", "ALGO123")
    settings.validate_live_config(live=True)


def test_live_trading_defaults_false():
    assert settings.LIVE_TRADING is False


# --------------------------------------------------------------- symbols


def test_every_instrument_fully_specified():
    for name, info in symbols.INSTRUMENTS.items():
        assert name in symbols.LOT_SIZES
        assert name in symbols.POINT_VALUES
        assert name in symbols.ATR_LIMITS
        assert name in symbols.EXPIRY_RULES
        assert symbols.cluster_of(name) == info["cluster"]
        if info["mini"]:
            assert info["mini"] in symbols.LOT_SIZES
            assert info["mini"] in symbols.POINT_VALUES


def test_active_symbols_default_all_five():
    assert symbols.ACTIVE_SYMBOLS == list(symbols.INSTRUMENTS)


def test_mini_toggle(monkeypatch):
    monkeypatch.setattr(symbols, "USE_MINI_CONTRACTS", False)
    assert symbols.active_symbol("GOLD") == "GOLD"
    monkeypatch.setattr(symbols, "USE_MINI_CONTRACTS", True)
    assert symbols.active_symbol("GOLD") == "GOLDM"
    assert symbols.active_symbol("COPPER") == "COPPER"  # no mini -> standard


def test_mini_maps_to_base_cluster():
    assert symbols.cluster_of("GOLDM") == "PRECIOUS_METALS"
    assert symbols.cluster_of("CRUDEOILM") == "ENERGY"


# -------------------------------------------------------------- database


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    models.init_db(path)
    return path


def test_init_db_creates_all_tables(db):
    with sqlite3.connect(db) as c:
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"trades", "daily_summary", "bot_state", "decision_log"} <= tables


def test_buy_trade_pnl_uses_point_value(db):
    # CRUDEOIL point value = 100. 2 lots, +50 points -> +10,000 rupees.
    tid = models.log_trade("CRUDEOIL", "BUY", 2, 6000.0, 5950.0, 6100.0,
                           "supertrend", db_path=db)
    pnl = models.close_trade(tid, 6050.0, "TAKE_PROFIT", db_path=db)
    assert pnl == pytest.approx(50 * 2 * 100)


def test_sell_trade_pnl_negated(db):
    # Short GOLD (point value 100), 1 lot, price rises 20 -> -2,000 rupees.
    tid = models.log_trade("GOLD", "SELL", 1, 72000.0, 72200.0, 71600.0,
                           "ema_trend", db_path=db)
    pnl = models.close_trade(tid, 72020.0, "STOP_LOSS", db_path=db)
    assert pnl == pytest.approx(-20 * 1 * 100)


def test_open_positions_and_close_lifecycle(db):
    tid = models.log_trade("SILVER", "BUY", 1, 90000.0, 89500.0, 91000.0,
                           "supertrend_or_meanrev", db_path=db)
    open_pos = models.get_open_positions(db_path=db)
    assert len(open_pos) == 1 and open_pos[0]["id"] == tid
    models.close_trade(tid, 91000.0, "TAKE_PROFIT", db_path=db)
    assert models.get_open_positions(db_path=db) == []


def test_performance_summary(db):
    t1 = models.log_trade("CRUDEOIL", "BUY", 1, 6000, 5950, 6100, "orb",
                          db_path=db)
    models.close_trade(t1, 6100.0, "TAKE_PROFIT", db_path=db)  # +10,000
    t2 = models.log_trade("GOLD", "BUY", 1, 72000, 71800, 72400, "ema_trend",
                          db_path=db)
    models.close_trade(t2, 71800.0, "STOP_LOSS", db_path=db)   # -20,000
    s = models.get_performance_summary(db_path=db)
    assert s["total_trades"] == 2
    assert s["win_rate"] == pytest.approx(50.0)
    assert s["total_pnl"] == pytest.approx(-10000)
    assert s["profit_factor"] == pytest.approx(0.5)
    assert s["by_exit"]["STOP_LOSS"] == pytest.approx(-20000)


def test_bot_state_halt_flag(db):
    assert models.is_halted(db_path=db) is False
    models.set_halted(True, db_path=db)
    assert models.is_halted(db_path=db) is True
    models.set_halted(False, db_path=db)
    assert models.is_halted(db_path=db) is False


def test_decision_log_append(db):
    models.log_decision("candidate_signal", "CRUDEOIL", "pm", "APPROVE",
                        "test rationale", db_path=db)
    with sqlite3.connect(db) as c:
        n = c.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0]
    assert n == 1


# -------------------------------------------------------------- telegram


def test_telegram_noop_without_token(monkeypatch):
    from notifications import telegram as tg
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "")

    def _boom(*a, **k):
        raise AssertionError("no HTTP call should happen when disabled")

    monkeypatch.setattr(tg._SESSION, "post", _boom)
    assert tg.send_message("hello") is False


def test_telegram_sends_when_configured(monkeypatch):
    from notifications import telegram as tg
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "123")

    class _Resp:
        status_code = 200
        def json(self):
            return {"ok": True}

    monkeypatch.setattr(tg._SESSION, "post", lambda *a, **k: _Resp())
    assert tg.send_message("hello") is True


def test_telegram_dedupe_cooldown(monkeypatch):
    """Repeating alerts (broker outage loop) page the owner once, not 30x
    an hour — regression for the 2026-07-14 Telegram flood."""
    from notifications import telegram as tg
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(tg, "_last_sent", {})
    calls = {"n": 0}

    class _Resp:
        status_code = 200
        def json(self):
            return {"ok": True}

    def _post(*a, **k):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(tg._SESSION, "post", _post)
    assert tg.send_message("boom", dedupe_key="k1") is True
    assert tg.send_message("boom again", dedupe_key="k1") is False  # deduped
    assert calls["n"] == 1
    assert tg.send_message("other", dedupe_key="k2") is True  # new key sends
    assert tg.send_message("plain") is True                   # no key: always
    assert calls["n"] == 3


def test_telegram_retries_transient_failure(monkeypatch):
    from notifications import telegram as tg
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)

    calls = {"n": 0}

    class _Resp:
        status_code = 200
        def json(self):
            return {"ok": True}

    def _flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("transient")
        return _Resp()

    monkeypatch.setattr(tg._SESSION, "post", _flaky)
    assert tg.send_message("hello") is True
    assert calls["n"] == 2


def test_telegram_never_raises(monkeypatch):
    from notifications import telegram as tg
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)

    def _always_down(*a, **k):
        raise ConnectionError("network down")

    monkeypatch.setattr(tg._SESSION, "post", _always_down)
    assert tg.send_message("hello") is False  # swallowed, returns False
