"""Central configuration for the MCX trading bot.

Every tunable comes from environment variables (loaded from .env at import).
Conventions, reused from the proven Solana bot:

- Secrets default to empty string. An empty secret means "feature disabled"
  (e.g. Telegram silently no-ops) — never a crash.
- Money values (INITIAL_CAPITAL, MAX_DAILY_LOSS_PCT, MAX_DRAWDOWN_PCT) default
  to None and MUST be set before live trading. validate_live_config() is the
  gate: it raises ConfigError listing everything missing if live mode is
  requested while any are unset. Paper mode runs fine with all of them unset.
- LIVE_TRADING defaults to false and is the single paper->live switch.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


class ConfigError(RuntimeError):
    """Raised when configuration is invalid for the requested mode."""


def _opt_float(name: str) -> float | None:
    """Optional float env var: unset/blank -> None (the guarded placeholder)."""
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else None


def _float(name: str, default: str) -> float:
    return float(os.getenv(name, default).strip() or default)


def _int(name: str, default: str) -> int:
    return int(os.getenv(name, default).strip() or default)


def _bool(name: str, default: str = "false") -> bool:
    return (os.getenv(name, default).strip() or default).lower() == "true"


# ------------------------------------------------------------------- mode

LIVE_TRADING = _bool("LIVE_TRADING")  # the one-line paper->live switch

# Simulated capital for paper-mode sizing only. NOT real money and NOT used
# in live mode (live uses INITIAL_CAPITAL, which the user must set).
PAPER_CAPITAL = _float("PAPER_CAPITAL", "100000")

# ------------------------------------------------- secrets (empty = disabled)

ANGEL_API_KEY = os.getenv("ANGEL_API_KEY", "").strip()
ANGEL_CLIENT_ID = os.getenv("ANGEL_CLIENT_ID", "").strip()
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD", "").strip()
ANGEL_TOTP_KEY = os.getenv("ANGEL_TOTP_KEY", "").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# SEBI: every live order must carry the registered algo tag.
ALGO_ID_TAG = os.getenv("ALGO_ID_TAG", "").strip()

# ------------------------------- money placeholders (REQUIRED before live)

INITIAL_CAPITAL = _opt_float("INITIAL_CAPITAL")        # rupees
MAX_DAILY_LOSS_PCT = _opt_float("MAX_DAILY_LOSS_PCT")  # % of capital
MAX_DRAWDOWN_PCT = _opt_float("MAX_DRAWDOWN_PCT")      # % from equity peak

# ------------------------------------------------- trading discipline params

MAX_RISK_PER_TRADE_PCT = _float("MAX_RISK_PER_TRADE_PCT", "1.0")
MAX_TRADES_PER_DAY = _int("MAX_TRADES_PER_DAY", "5")
MIN_REWARD_RISK = _float("MIN_REWARD_RISK", "2.0")
MAX_POSITION_PCT = _float("MAX_POSITION_PCT", "10.0")
MAX_CLUSTER_PCT = _float("MAX_CLUSTER_PCT", "15.0")
ATR_SL_MULT = _float("ATR_SL_MULT", "1.5")

# Paper executor: simulated slippage applied to fills, % of price.
PAPER_SLIPPAGE_PCT = _float("PAPER_SLIPPAGE_PCT", "0.05")

# ------------------------------------------------------------------- paths

DB_FILE = Path(os.getenv("DB_FILE", "").strip() or PROJECT_ROOT / "mcx_bot.db")
LOG_DIR = PROJECT_ROOT / "logs"
STATE_DIR = PROJECT_ROOT / "state"

# ------------------------------------------------------------------- gate


def validate_live_config(live: bool | None = None) -> None:
    """Refuse live mode while any required value is unset.

    Reads module globals (not the environment) so runtime overrides and tests
    see a consistent view. No-op in paper mode.
    """
    if not (LIVE_TRADING if live is None else live):
        return

    missing: list[str] = []
    for name in ("INITIAL_CAPITAL", "MAX_DAILY_LOSS_PCT", "MAX_DRAWDOWN_PCT"):
        if globals()[name] is None:
            missing.append(name)
    for name in ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PASSWORD",
                 "ANGEL_TOTP_KEY"):
        if not globals()[name]:
            missing.append(name)
    if not globals()["ALGO_ID_TAG"]:
        missing.append("ALGO_ID_TAG (SEBI algo tag, mandatory on live orders)")

    if missing:
        raise ConfigError(
            "LIVE mode refused — set these in .env first (paper mode does "
            "not need them): " + ", ".join(missing)
        )
