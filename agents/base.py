"""Agent layer foundations.

DESIGN RULE (the whole safety model of the agentic layer): every agent has a
DETERMINISTIC core that produces its structured output from the DecisionContext
alone — paper mode runs with zero API keys and zero cost. When
LLM_AGENTS_ENABLED is true, ask_llm() may enrich the natural-language
rationale or argue for CAUTION, but LLM output can never approve what the
deterministic rules and the risk gate chain rejected.
"""

import logging
from dataclasses import dataclass, field

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class DecisionContext:
    """Everything the agents may look at, snapshotted by the orchestrator."""
    symbol: str
    signal: object                     # strategies.base.Signal
    regime: object                     # core.regime.Regime (primary TF)
    mtf: dict                          # core.regime.mtf_regime output
    volume_ratio: float = 1.0          # last bar vs 20-bar average
    open_positions: list = field(default_factory=list)
    capital: float = 0.0
    equity: float = 0.0
    days_to_expiry: int | None = None
    consecutive_losses: int = 0
    trades_today: int = 0
    weekday: int = 0                   # 0=Mon .. 6=Sun (informational)
    # Economic calendar (data/economic_calendar.py), scoped to this symbol:
    upcoming_events: list = field(default_factory=list)   # EconEvents inside
    #                                                       the blackout window
    events_today: list = field(default_factory=list)      # names, today
    calendar_source: str = ""          # finnhub / static-weekly


@dataclass
class AgentOutput:
    agent: str
    stance: str                        # BULLISH / BEARISH / NEUTRAL / VETO
    confidence: float                  # 0..1
    rationale: str
    fields: dict = field(default_factory=dict)


class Agent:
    name = "agent"

    def run(self, ctx: DecisionContext) -> AgentOutput:
        raise NotImplementedError


def ask_llm(prompt: str, system: str = "") -> str | None:
    """Optional enrichment. Returns None when disabled or on ANY failure —
    callers must always have a deterministic answer already."""
    if not (settings.LLM_AGENTS_ENABLED and settings.ANTHROPIC_API_KEY):
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",  # cheap: rationale-only duty
            max_tokens=300,
            system=system or "You are a commodity trading analyst. "
                             "Reply in under 80 words.",
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text
    except Exception as exc:
        logger.warning("LLM enrichment unavailable: %s", exc)
        return None
