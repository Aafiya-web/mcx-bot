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
# ₹5L default: standard MCX lots carry ₹30k-60k margin each, so a smaller
# figure sizes zero lots on most instruments. For small-account simulation
# set USE_MINI_CONTRACTS=true instead of lowering this.
PAPER_CAPITAL = _float("PAPER_CAPITAL", "500000")

# ------------------------------------------------- secrets (empty = disabled)

ANGEL_API_KEY = os.getenv("ANGEL_API_KEY", "").strip()
ANGEL_CLIENT_ID = os.getenv("ANGEL_CLIENT_ID", "").strip()
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD", "").strip()
ANGEL_TOTP_KEY = os.getenv("ANGEL_TOTP_KEY", "").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Optional LLM enrichment for the agent layer. Disabled by default: every
# agent has a deterministic core, and LLM output may only annotate/tighten a
# decision — it can NEVER bypass the risk gate chain.
LLM_AGENTS_ENABLED = _bool("LLM_AGENTS_ENABLED")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()  # macro 2nd opinion

# Finnhub economic calendar (data/economic_calendar.py). Empty = the static
# weekly fallback table is used (offline-safe, EIA Wed/Thu only).
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
# New entries on an instrument are blocked while a high-impact event for it
# is within this many minutes ahead (enforced in the risk gate chain).
EVENT_BLACKOUT_MINUTES = _int("EVENT_BLACKOUT_MINUTES", "120")

# ------------------------------------------------- positional (overnight)
# BASE symbols allowed to HOLD overnight when the trend earns it — see
# Engine._may_hold_overnight for the eligibility rules (1H trend intact,
# profit cushion, no imminent event, not near expiry). Everything else,
# and every ineligible position, squares off daily. Empty = fully intraday.
POSITIONAL_SYMBOLS = [s.strip().upper() for s in
                      os.getenv("POSITIONAL_SYMBOLS", "GOLD,COPPER")
                      .split(",") if s.strip()]
# Minimum open profit (in R multiples) before a position may be carried
# overnight — the profit cushion that absorbs opening-gap risk.
OVERNIGHT_MIN_R = float(os.getenv("OVERNIGHT_MIN_R", "0.5"))

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
# Margin-utilisation caps (MCX futures are leveraged, so caps are expressed
# against ESTIMATED MARGIN, not notional — see symbols.MARGIN_PCT_ESTIMATE):
MAX_POSITION_MARGIN_PCT = _float("MAX_POSITION_MARGIN_PCT", "30.0")
MAX_CLUSTER_MARGIN_PCT = _float("MAX_CLUSTER_MARGIN_PCT", "50.0")
ATR_SL_MULT = _float("ATR_SL_MULT", "1.5")

# Bull score must beat bear score by this margin before the Trader agent
# turns a debate into a proposal (agentic-layer sensitivity knob).
AGENT_APPROVAL_MARGIN = _float("AGENT_APPROVAL_MARGIN", "1.0")

# Paper executor: simulated slippage applied to fills, % of price.
PAPER_SLIPPAGE_PCT = _float("PAPER_SLIPPAGE_PCT", "0.05")

# ---------------------------------------------------------------- dashboard

DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1").strip()
DASHBOARD_PORT = _int("DASHBOARD_PORT", "5001")  # Solana bot owns 5000
# Required to expose the dashboard beyond localhost (HTTP Basic Auth,
# any username). Empty password forces DASHBOARD_HOST back to 127.0.0.1.
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()
if not DASHBOARD_PASSWORD and DASHBOARD_HOST not in ("127.0.0.1",
                                                     "localhost"):
    DASHBOARD_HOST = "127.0.0.1"  # never public without a password

# ------------------------------------------------------------------- paths

DB_FILE = Path(os.getenv("DB_FILE", "").strip() or PROJECT_ROOT / "mcx_bot.db")
LOG_DIR = PROJECT_ROOT / "logs"
STATE_DIR = PROJECT_ROOT / "state"
# A bare clone must run: these are gitignored at the content level, so
# they may not exist (the systemd unit's FileHandler needs logs/ at boot).
LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)

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
