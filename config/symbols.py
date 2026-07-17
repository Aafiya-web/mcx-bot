"""The 5-instrument MCX lineup and all per-symbol static data.

Single source of truth for what the bot trades and how each instrument is
classified. Values follow the skill files (mcx-signal-analyzer,
mcx-risk-manager, mcx-correlation-filter, mcx-contract-monitor,
mcx-regime-detector).

VERIFY AT STEP 2 (broker research): lot sizes, point values, and mini-contract
availability change with MCX circulars — confirm against current contract
specs before any live order. Symbol tokens are NEVER stored here; they are
resolved daily via api.searchScrip() because they change each contract cycle.
"""

import os

# Strategy keys map to strategies/ modules built at step 5.
INSTRUMENTS: dict[str, dict] = {
    "CRUDEOIL": {
        "cluster": "ENERGY",
        "strategy": "orb_then_supertrend",
        "timeframe": "FIFTEEN_MINUTE",
        "mini": "CRUDEOILM",
    },
    "NATURALGAS": {
        "cluster": "ENERGY",
        "strategy": "momentum_breakout",
        "timeframe": "FIFTEEN_MINUTE",
        "mini": "NATGASMINI",
    },
    "GOLD": {
        "cluster": "PRECIOUS_METALS",
        "strategy": "ema_trend",
        "timeframe": "ONE_HOUR",
        "mini": "GOLDM",
    },
    "SILVER": {
        "cluster": "PRECIOUS_METALS",
        "strategy": "supertrend_or_meanrev",
        "timeframe": "FIFTEEN_MINUTE",
        "mini": "SILVERM",
    },
    "COPPER": {
        "cluster": "BASE_METALS",
        "strategy": "supertrend",
        "timeframe": "ONE_HOUR",
        "mini": None,  # no liquid mini contract — falls back to standard
    },
}

# Contract size in the commodity's own units (barrels, kg, mmBtu, grams).
LOT_SIZES: dict[str, int] = {
    "CRUDEOIL": 100,      # barrels
    "CRUDEOILM": 10,      # barrels
    "GOLD": 1000,         # grams (1 kg)
    "GOLDM": 100,         # grams
    "SILVER": 30,         # kg
    "SILVERM": 5,         # kg
    "NATURALGAS": 1250,   # mmBtu
    "NATGASMINI": 250,    # mmBtu (VERIFIED live 2026-07-17: the natural
                          # gas mini scrip is NATGASMINI, not NATURALGASM)
    "COPPER": 2500,       # kg
    "ZINC": 5000,         # kg
    "ALUMINIUM": 5000,    # kg
}

# Rupees of P&L per 1-point move in the quoted price, per lot. This is what
# position sizing and P&L math must use — it differs from LOT_SIZES where the
# quote unit differs from the contract unit (GOLD quotes per 10g, lot = 1kg).
POINT_VALUES: dict[str, int] = {
    "CRUDEOIL": 100,      # quoted per barrel
    "CRUDEOILM": 10,
    "GOLD": 100,          # quoted per 10g, 1kg lot -> 100 x 10g
    "GOLDM": 10,
    "SILVER": 30,         # quoted per kg
    "SILVERM": 5,
    "NATURALGAS": 1250,   # quoted per mmBtu
    "NATGASMINI": 250,
    "COPPER": 2500,       # quoted per kg
    "ZINC": 5000,
    "ALUMINIUM": 5000,
}

# Approximate initial margin as % of contract notional (SPAN + exposure).
# Used ONLY for the margin-utilisation caps in risk/ — paper sizing sanity,
# never for real margin decisions. VERIFY against the broker's margin
# calculator at step 2 research / before live; MCX revises these often.
MARGIN_PCT_ESTIMATE: dict[str, float] = {
    "CRUDEOIL": 10.0, "CRUDEOILM": 10.0,
    "NATURALGAS": 12.0, "NATGASMINI": 12.0,
    "GOLD": 8.0, "GOLDM": 8.0,
    "SILVER": 10.0, "SILVERM": 10.0,
    "COPPER": 9.0,
    "ZINC": 9.0, "ALUMINIUM": 9.0,
}

# Same-direction risk is never stacked within a cluster (mcx-correlation-filter).
CORRELATION_CLUSTERS: dict[str, dict] = {
    "ENERGY": {
        "members": ["CRUDEOIL", "NATURALGAS"],
        "note": "Both driven by energy demand, EIA data, geopolitics",
        "correlation": 0.6,
    },
    "PRECIOUS_METALS": {
        "members": ["GOLD", "SILVER"],
        "note": "Both driven by USD, Fed policy, risk sentiment. "
                "Silver = higher beta version of gold",
        "correlation": 0.8,
    },
    "BASE_METALS": {
        "members": ["COPPER", "ZINC", "ALUMINIUM"],
        "note": "Industrial demand, China growth, USD",
        "correlation": 0.65,
    },
}

# Expiry behaviour per commodity (mcx-contract-monitor). roll_days = how many
# days before expiry the position is rolled to the next contract.
EXPIRY_RULES: dict[str, dict] = {
    "CRUDEOIL": {"type": "last_thursday_minus_1", "roll_days": 3},
    "GOLD": {"type": "5th_last_day", "roll_days": 5},
    "SILVER": {"type": "5th_last_day", "roll_days": 5},
    "NATURALGAS": {"type": "last_thursday", "roll_days": 3},
    "COPPER": {"type": "last_wednesday", "roll_days": 3},
}

# ATR as % of price must sit inside these bands or the regime detector
# vetoes trading (mcx-regime-detector).
#
# FLOORS RECALIBRATED 2026-07-16 from live 15-min data (~6.4 sessions,
# 373 bars/symbol). The skill's original floors were daily-bar values:
# on 15-min bars they rejected ~72% of GOLD, ~65% of NATURALGAS, ~50%
# of SILVER — and 100% of COPPER (floor 0.2 vs observed P90 0.198),
# making it untradeable by construction. New floors sit just below each
# instrument's observed P10, restoring the floor's actual job: skip DEAD
# tape, not normal tape. CEILINGS unchanged (the too-wild-to-size guard).
# Observed P10/P50: CRUDE .43/.55, NG .35/.45, GOLD .13/.18,
# SILVER .24/.30, COPPER .15/.17.
ATR_LIMITS: dict[str, dict] = {
    "CRUDEOIL": {"min": 0.3, "max": 3.0},    # floor already sane; kept
    "GOLD": {"min": 0.10, "max": 1.5},
    "SILVER": {"min": 0.20, "max": 2.5},
    "NATURALGAS": {"min": 0.30, "max": 5.0},
    "COPPER": {"min": 0.12, "max": 2.0},
}

USE_MINI_CONTRACTS = (
    os.getenv("USE_MINI_CONTRACTS", "false").strip().lower() == "true"
)


def _parse_active() -> list[str]:
    raw = os.getenv("ACTIVE_SYMBOLS", "").strip()
    if not raw:
        return list(INSTRUMENTS)
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    unknown = [s for s in symbols if s not in INSTRUMENTS]
    if unknown:
        raise ValueError(
            f"ACTIVE_SYMBOLS contains unknown instruments {unknown}; "
            f"valid: {list(INSTRUMENTS)}"
        )
    return symbols


ACTIVE_SYMBOLS: list[str] = _parse_active()


def active_symbol(base: str) -> str:
    """Tradeable symbol for a base instrument, honoring the mini toggle."""
    info = INSTRUMENTS[base]
    if USE_MINI_CONTRACTS and info["mini"]:
        return info["mini"]
    return base


def base_of(symbol: str) -> str:
    """Base instrument for a traded symbol (mini variants map back)."""
    for name, info in INSTRUMENTS.items():
        if info["mini"] == symbol:
            return name
    return symbol


def cluster_of(symbol: str) -> str | None:
    """Correlation cluster for a symbol (mini variants map to their base)."""
    base = base_of(symbol)
    for cluster, data in CORRELATION_CLUSTERS.items():
        if base in data["members"]:
            return cluster
    return None
