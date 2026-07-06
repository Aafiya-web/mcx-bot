"""Main Orchestrator — the conductor of the agentic layer.

Plain-Python state machine (deliberately not LangGraph: fewer moving parts
on a 1GB VM, and the pipeline is a fixed linear sequence). Fired ONLY when
the deterministic layer flags something worth deliberating — a candidate
signal, a regime shift, or a scheduled review — never per tick.

Pipeline:  analysts (technical / macro / regime)
        -> bull vs bear debate
        -> trader proposal
        -> risk team (owns the binding gate chain)
        -> portfolio manager (final gate)

Every stage's structured output is persisted to decision_log, so any model
(or human) can replay exactly why each trade did or didn't happen.
"""

import json
import logging
from dataclasses import dataclass, field

from agents.analysts import MacroAnalyst, RegimeAnalyst, TechnicalAnalyst
from agents.base import AgentOutput, DecisionContext
from agents.debate import BearResearcher, BullResearcher, TraderAgent
from agents.portfolio_manager import PortfolioManager
from agents.reflection import ReflectionAgent
from agents.risk_team import RiskTeam
from database import models

logger = logging.getLogger(__name__)


@dataclass
class Decision:
    approved: bool
    action: str = "HOLD"
    lots: int = 0
    rationale: str = ""
    stages: dict[str, AgentOutput] = field(default_factory=dict)


class Orchestrator:
    def __init__(self, daily, guard, db_path=None):
        self.db = db_path
        self.technical = TechnicalAnalyst()
        self.macro = MacroAnalyst()
        self.regime_analyst = RegimeAnalyst()
        self.bull = BullResearcher()
        self.bear = BearResearcher()
        self.trader = TraderAgent()
        self.risk_team = RiskTeam(daily, guard)
        self.pm = PortfolioManager()
        self.reflection = ReflectionAgent(db_path)

    def _log(self, trigger: str, symbol: str, out: AgentOutput) -> None:
        models.log_decision(
            trigger, symbol, out.agent, out.stance, out.rationale,
            json.dumps(out.fields, default=str), self.db)

    def evaluate(self, ctx: DecisionContext,
                 trigger: str = "candidate_signal") -> Decision:
        """Run the full pipeline on one candidate. Returns the gated call."""
        stages: dict[str, AgentOutput] = {}

        def run(agent, **kwargs) -> AgentOutput:
            out = agent.run(ctx, **kwargs)
            stages[out.agent] = out
            self._log(trigger, ctx.symbol, out)
            return out

        technical = run(self.technical)
        macro = run(self.macro)
        regime = run(self.regime_analyst)
        bull = run(self.bull)
        bear = run(self.bear)
        trader = run(self.trader, bull=bull, bear=bear)
        risk = run(self.risk_team, macro=macro)
        pm = run(self.pm, technical=technical, regime=regime,
                 trader=trader, risk=risk)

        decision = Decision(
            approved=pm.stance == "APPROVE",
            action=pm.fields.get("action", "HOLD"),
            lots=pm.fields.get("lots", 0),
            rationale=pm.rationale,
            stages=stages,
        )
        logger.info("Orchestrator[%s] %s: %s", trigger, ctx.symbol,
                    decision.rationale)
        return decision

    def reflect(self) -> AgentOutput:
        """Scheduled review — adapts bounded params, logs everything."""
        out = self.reflection.run(None)
        self._log("scheduled_review", "*", out)
        return out
