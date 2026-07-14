"""Step 2 (Broker) tests: paper executor fills, SL-M backstop simulation,
and — critically — that the live path is unreachable in paper mode."""

import pytest

from broker import order_manager as om
from broker.order_manager import (LiveExecutor, PaperExecutor,
                                  get_order_manager)
from config import settings


@pytest.fixture
def prices():
    book = {"CRUDEOIL": 6000.0, "GOLD": 72000.0}
    return book


@pytest.fixture
def paper(prices, monkeypatch):
    monkeypatch.setattr(settings, "PAPER_SLIPPAGE_PCT", 0.1)
    return PaperExecutor(lambda s: prices[s])


# ------------------------------------------------------------ paper fills


def test_market_buy_slips_up(paper):
    fill = paper.place_market_order("CRUDEOIL", "BUY", 2, tag="t")
    assert fill.status == "FILLED"
    assert fill.price == pytest.approx(6000 * 1.001)  # adverse for buyer
    assert fill.qty == 2


def test_market_sell_slips_down(paper):
    fill = paper.place_market_order("CRUDEOIL", "SELL", 1)
    assert fill.price == pytest.approx(6000 * 0.999)  # adverse for seller


def test_sl_m_rests_until_breach_long(paper, prices):
    # Long CRUDEOIL protected by a SELL stop at 5950.
    order = paper.place_sl_market_order("CRUDEOIL", "SELL", 1, trigger=5950.0)
    assert order.status == "PENDING"
    assert paper.process_pending() == []          # 6000 > 5950: no fire

    prices["CRUDEOIL"] = 5940.0                   # breach
    fills = paper.process_pending()
    assert len(fills) == 1
    assert fills[0].order_id == order.order_id
    assert fills[0].price == pytest.approx(5940 * 0.999)  # market, slipped
    assert paper.pending == {}


def test_sl_m_rests_until_breach_short(paper, prices):
    # Short GOLD covered by a BUY stop at 72200.
    paper.place_sl_market_order("GOLD", "BUY", 1, trigger=72200.0)
    assert paper.process_pending() == []
    prices["GOLD"] = 72250.0
    fills = paper.process_pending()
    assert len(fills) == 1 and fills[0].side == "BUY"


def test_cancel_resting_order(paper):
    order = paper.place_sl_market_order("CRUDEOIL", "SELL", 1, trigger=5950.0)
    assert paper.cancel_order(order.order_id) is True
    assert paper.cancel_order(order.order_id) is False
    assert paper.process_pending() == []


# ----------------------------------------------------------- live gating


def test_factory_returns_paper_by_default(prices):
    assert settings.LIVE_TRADING is False
    mgr = get_order_manager(lambda s: prices[s])
    assert isinstance(mgr, PaperExecutor)


def test_live_executor_refuses_paper_mode():
    with pytest.raises(settings.ConfigError, match="LIVE_TRADING is false"):
        LiveExecutor(contract_fn=lambda s: (s, "123"))


def test_live_executor_refuses_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "LIVE_TRADING", True)
    monkeypatch.setattr(settings, "INITIAL_CAPITAL", None)
    with pytest.raises(settings.ConfigError, match="LIVE mode refused"):
        LiveExecutor(contract_fn=lambda s: (s, "123"))


@pytest.fixture
def live(monkeypatch):
    """Fully-configured live executor for offline param-building tests."""
    for name, val in [("LIVE_TRADING", True), ("INITIAL_CAPITAL", 100000.0),
                      ("MAX_DAILY_LOSS_PCT", 2.0), ("MAX_DRAWDOWN_PCT", 10.0),
                      ("ANGEL_API_KEY", "k"), ("ANGEL_CLIENT_ID", "c"),
                      ("ANGEL_PASSWORD", "p"), ("ANGEL_TOTP_KEY", "t"),
                      ("ALGO_ID_TAG", "MCXBOT01")]:
        monkeypatch.setattr(settings, name, val)
    # contract_fn maps the base name to the active contract month + token.
    return LiveExecutor(
        contract_fn=lambda s: ("CRUDEOIL26JULFUT", "424242"))


def test_live_params_carry_algo_tag_and_mcx(live):
    p = live._build_params("CRUDEOIL", "BUY", 1, "MARKET", "NORMAL")
    assert p["ordertag"] == "MCXBOT01"
    assert p["exchange"] == "MCX"
    assert p["tradingsymbol"] == "CRUDEOIL26JULFUT"  # the ACTIVE contract
    assert p["symboltoken"] == "424242"
    assert "triggerprice" not in p


def test_live_sl_m_params(live):
    p = live._build_params("CRUDEOIL", "SELL", 1,
                           "STOPLOSS_MARKET", "STOPLOSS", trigger=5950.0)
    assert p["ordertype"] == "STOPLOSS_MARKET"
    assert p["variety"] == "STOPLOSS"
    assert p["triggerprice"] == 5950.0


def test_live_order_refused_without_algo_tag(live, monkeypatch):
    monkeypatch.setattr(settings, "ALGO_ID_TAG", "")
    with pytest.raises(settings.ConfigError, match="algo tag"):
        live._build_params("CRUDEOIL", "BUY", 1, "MARKET", "NORMAL")


# ------------------------------------------------------------- auth module


def test_auth_importable_and_guarded_without_creds():
    from broker import auto_login
    assert settings.ANGEL_API_KEY == ""  # nothing configured in tests
    with pytest.raises(settings.ConfigError, match="ANGEL_API_KEY"):
        auto_login.login()


def test_auth_retry_decorator_recovers(monkeypatch):
    from broker import auto_login

    recovered = {"refresh": 0}
    monkeypatch.setattr(auto_login, "refresh_token",
                        lambda: recovered.__setitem__("refresh", 1))
    calls = {"n": 0}

    @auto_login.with_auth_retry
    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("AG8001: Invalid Token")
        return "ok"

    assert flaky() == "ok"
    assert recovered["refresh"] == 1


def test_is_auth_error_matches_real_world_shapes():
    """Regression for 2026-07-13 (the blind Monday): the matcher missed
    'Invalid Token' (capital T) and smartapi's KeyError('status')."""
    from broker.auto_login import _is_auth_error

    assert _is_auth_error(RuntimeError("Error: Invalid Token"))     # caps
    assert _is_auth_error(RuntimeError("errorcode AG8001"))
    assert _is_auth_error(KeyError("status"))                       # smartapi
    assert not _is_auth_error(RuntimeError("network unreachable"))
    assert not _is_auth_error(KeyError("data"))
