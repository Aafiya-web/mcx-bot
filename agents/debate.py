"""Bull vs Bear researchers + the Trader who consolidates the debate.

The dialectic stress-tests a candidate signal before capital is considered:
the Bull collects every supporting fact, the Bear every risk. The Trader
only proposes when the Bull wins by AGENT_APPROVAL_MARGIN.
"""

from agents.base import Agent, AgentOutput, DecisionContext
from config import settings
from config.symbols import cluster_of


class BullResearcher(Agent):
    name = "bull"

    def run(self, ctx: DecisionContext) -> AgentOutput:
        pts, notes = 0.0, []
        if ctx.signal.action in ("BUY", "SELL"):
            pts += 1
            notes.append(f"clean {ctx.signal.strategy} signal")
        if ctx.regime.regime in ("TRENDING", "STRONG_TREND"):
            pts += 1.5 if ctx.regime.regime == "STRONG_TREND" else 1.0
            notes.append(f"{ctx.regime.regime} regime")
        if ctx.mtf.get("consensus"):
            pts += 1
            notes.append("timeframes agree")
        if ctx.signal.rr >= 2.5:
            pts += 0.5
            notes.append(f"generous RR {ctx.signal.rr}")
        if ctx.volume_ratio >= 1.5:
            pts += 1
            notes.append(f"volume {ctx.volume_ratio:.1f}x")
        return AgentOutput(self.name, "BULLISH", min(pts / 5, 1.0),
                           "; ".join(notes) or "nothing to argue",
                           {"score": pts})


class BearResearcher(Agent):
    name = "bear"

    def run(self, ctx: DecisionContext) -> AgentOutput:
        pts, notes = 0.0, []
        if ctx.regime.regime in ("RANGING", "NEUTRAL", "UNKNOWN", "SQUEEZE"):
            pts += 2
            notes.append(f"weak regime ({ctx.regime.regime})")
        if not ctx.mtf.get("consensus"):
            pts += 1
            notes.append("timeframes disagree")
        if ctx.days_to_expiry is not None and ctx.days_to_expiry <= 3:
            pts += 2
            notes.append(f"expiry in {ctx.days_to_expiry}d")
        if ctx.consecutive_losses >= 2:
            pts += 1
            notes.append(f"{ctx.consecutive_losses} consecutive losses")
        if ctx.volume_ratio < 1.0:
            pts += 1
            notes.append("below-average volume")
        cluster = cluster_of(ctx.symbol)
        held = [p for p in ctx.open_positions
                if cluster_of(p["symbol"]) == cluster]
        if held:
            pts += 1
            notes.append(f"existing {cluster} exposure "
                         f"({held[0]['symbol']})")
        return AgentOutput(self.name, "BEARISH", min(pts / 5, 1.0),
                           "; ".join(notes) or "no material risks found",
                           {"score": pts})


class TraderAgent(Agent):
    """Consolidates the debate into a concrete go/no-go proposal. Size is
    NOT decided here — the risk gate chain owns sizing."""

    name = "trader"

    def run(self, ctx: DecisionContext, bull: AgentOutput = None,
            bear: AgentOutput = None) -> AgentOutput:
        bull_score = bull.fields["score"] if bull else 0
        bear_score = bear.fields["score"] if bear else 0
        margin = settings.AGENT_APPROVAL_MARGIN
        edge = bull_score - bear_score

        if ctx.signal.action not in ("BUY", "SELL"):
            return AgentOutput(self.name, "NEUTRAL", 1.0,
                               "no actionable signal", {"proposed": False})
        if edge < margin:
            return AgentOutput(
                self.name, "NEUTRAL", 0.7,
                f"debate too close: bull {bull_score:.1f} vs bear "
                f"{bear_score:.1f} (need +{margin})", {"proposed": False})

        return AgentOutput(
            self.name,
            "BULLISH" if ctx.signal.action == "BUY" else "BEARISH",
            min(edge / 5, 1.0),
            f"propose {ctx.signal.action} {ctx.symbol} @ {ctx.signal.entry:.1f}, "
            f"stop {ctx.signal.stop_loss:.1f}, target {ctx.signal.target:.1f} "
            f"(bull {bull_score:.1f} vs bear {bear_score:.1f})",
            {"proposed": True, "action": ctx.signal.action})
