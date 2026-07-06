"""Reflection / Optimizer agent — bounded, logged self-adaptation.

Reviews recent performance and adjusts tunable parameters INSIDE hard
bounds. Every change lands in the param_changes changelog (old value, new
value, reason) so it can be audited and rolled back. Safety ceilings
(money limits, the live gate, hard-stop policy) are not in the registry and
therefore can never be touched by adaptation.
"""

import logging

from agents.base import Agent, AgentOutput, DecisionContext
from database import models

logger = logging.getLogger(__name__)

# The ONLY knobs adaptation may turn, each with hard bounds.
# target: (module, attribute) so changes apply to the running process.
PARAM_BOUNDS: dict[str, dict] = {
    "ADX_TRENDING": {"lo": 20.0, "hi": 30.0,
                     "target": ("core.regime", "ADX_TRENDING")},
    "ADX_RANGING": {"lo": 15.0, "hi": 22.0,
                    "target": ("core.regime", "ADX_RANGING")},
    "ATR_SL_MULT": {"lo": 1.0, "hi": 2.5,
                    "target": ("config.settings", "ATR_SL_MULT")},
    "AGENT_APPROVAL_MARGIN": {"lo": 0.5, "hi": 3.0,
                              "target": ("config.settings",
                                         "AGENT_APPROVAL_MARGIN")},
}


def apply_param(param: str, value: float, reason: str,
                db_path=None) -> tuple[bool, float]:
    """Clamp to bounds, apply to the live module, log to the changelog.
    Returns (changed, applied_value)."""
    import importlib

    spec = PARAM_BOUNDS.get(param)
    if spec is None:
        logger.error("Adaptation refused: %s is not an adaptable param",
                     param)
        return False, 0.0
    clamped = max(spec["lo"], min(spec["hi"], value))
    module = importlib.import_module(spec["target"][0])
    old = getattr(module, spec["target"][1])
    if clamped == old:
        return False, old
    setattr(module, spec["target"][1], clamped)
    models.log_param_change(param, old, clamped,
                            reason + (" [clamped to bounds]"
                                      if clamped != value else ""),
                            db_path)
    logger.info("Adapted %s: %s -> %s (%s)", param, old, clamped, reason)
    return True, clamped


class ReflectionAgent(Agent):
    """Runs after closes / on schedule. Conservative playbook: it PROPOSES
    small nudges based on aggregate stats; anything structural goes to the
    human via the daily briefing instead."""

    name = "reflection"

    def __init__(self, db_path=None):
        self.db = db_path

    def run(self, ctx: DecisionContext = None) -> AgentOutput:
        stats = models.get_performance_summary(days=7, db_path=self.db)
        applied: list[str] = []

        if stats["total_trades"] >= 10:
            import core.regime as regime_mod
            # Too many losing trades in weak regimes -> demand stronger trend.
            if stats["win_rate"] < 45:
                ok, val = apply_param(
                    "ADX_TRENDING", regime_mod.ADX_TRENDING + 1.0,
                    f"7d win rate {stats['win_rate']:.0f}% < 45% — "
                    f"tighten trend filter", self.db)
                if ok:
                    applied.append(f"ADX_TRENDING -> {val}")
            # Healthy performance -> relax toward defaults, small steps.
            elif stats["win_rate"] > 55 and stats["profit_factor"] > 1.5:
                ok, val = apply_param(
                    "ADX_TRENDING", regime_mod.ADX_TRENDING - 0.5,
                    f"7d WR {stats['win_rate']:.0f}%, PF "
                    f"{stats['profit_factor']:.2f} — slightly relax filter",
                    self.db)
                if ok:
                    applied.append(f"ADX_TRENDING -> {val}")

        rationale = (f"7d: {stats['total_trades']} trades, WR "
                     f"{stats['win_rate']:.0f}%, PF "
                     f"{stats['profit_factor']:.2f}. "
                     + (f"Adapted: {', '.join(applied)}" if applied
                        else "No parameter changes."))
        return AgentOutput(self.name, "NEUTRAL", 0.8, rationale,
                           {"applied": applied, "stats": stats})
