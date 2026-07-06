"""Portfolio Manager — the final gate. Only an APPROVE from here reaches
the execution layer, and only with the risk team's (possibly reduced) size."""

from agents.base import Agent, AgentOutput, DecisionContext


class PortfolioManager(Agent):
    name = "pm"

    def run(self, ctx: DecisionContext, technical: AgentOutput = None,
            regime: AgentOutput = None, trader: AgentOutput = None,
            risk: AgentOutput = None) -> AgentOutput:
        def reject(why: str) -> AgentOutput:
            return AgentOutput(self.name, "REJECT", 1.0, why, {"lots": 0})

        if regime and regime.stance == "VETO":
            return reject(f"regime analyst veto: {regime.rationale}")
        if not (trader and trader.fields.get("proposed")):
            return reject(f"no trade proposal: "
                          f"{trader.rationale if trader else 'missing'}")
        if not (risk and risk.stance != "VETO" and risk.fields.get("lots", 0) > 0):
            return reject(f"risk team: {risk.rationale if risk else 'missing'}")
        if technical and technical.stance == "NEUTRAL":
            return reject(f"technical conviction too low: "
                          f"{technical.rationale}")

        lots = risk.fields["lots"]
        return AgentOutput(
            self.name, "APPROVE", min(trader.confidence, risk.confidence),
            f"APPROVED {ctx.signal.action} {ctx.symbol} x{lots}: "
            f"{trader.rationale}",
            {"lots": lots, "action": ctx.signal.action})
