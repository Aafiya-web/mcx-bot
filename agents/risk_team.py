"""Risk Management Team — aggressive / neutral / conservative perspectives.

Neutral runs the definitive gate chain (regime -> sizing -> correlation ->
portfolio guard). Aggressive may never add size beyond the chain's answer;
conservative may only cut or veto. The team verdict is the MOST restrictive.
"""

from agents.base import Agent, AgentOutput, DecisionContext
from risk.gate_chain import run_gate_chain


class RiskTeam(Agent):
    name = "risk_team"

    def __init__(self, daily, guard):
        self.daily = daily
        self.guard = guard

    def run(self, ctx: DecisionContext,
            macro: AgentOutput = None) -> AgentOutput:
        # NEUTRAL: the binding gate chain (includes the event blackout).
        gate = run_gate_chain(ctx.signal, ctx.symbol, ctx.regime,
                              ctx.open_positions, ctx.capital, self.daily,
                              self.guard, ctx.equity,
                              upcoming_events=ctx.upcoming_events)
        if not gate.approved:
            return AgentOutput(self.name, "VETO", 1.0,
                               f"gate chain: {gate.rejection_reason}",
                               {"lots": 0, "gate": gate.checks})

        lots = gate.lots
        notes = [f"neutral: gate chain approved {lots} lot(s)"]

        # CONSERVATIVE: may only cut or veto.
        if ctx.days_to_expiry is not None and ctx.days_to_expiry <= 1:
            return AgentOutput(self.name, "VETO", 1.0,
                               "conservative veto: expiry within 1 day",
                               {"lots": 0, "gate": gate.checks})
        if ctx.consecutive_losses >= 3:
            return AgentOutput(self.name, "VETO", 1.0,
                               "conservative veto: 3+ consecutive losses — "
                               "pause for the day",
                               {"lots": 0, "gate": gate.checks})
        if ctx.days_to_expiry is not None and ctx.days_to_expiry <= 3:
            lots = max(1, lots // 2)
            notes.append(f"conservative: expiry week — size halved to {lots}")
        if macro and macro.fields.get("risk_flags"):
            lots = max(1, lots // 2)
            notes.append(f"conservative: event day "
                         f"({', '.join(macro.fields['risk_flags'])}) — "
                         f"size halved to {lots}")

        # AGGRESSIVE: wants more, is not allowed more. Recorded for audit.
        notes.append("aggressive: would size up; capped at gate-chain size")

        return AgentOutput(self.name, "NEUTRAL", 0.9, "; ".join(notes),
                           {"lots": lots, "gate": gate.checks})
