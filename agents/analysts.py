"""Analyst agents: Technical, Macro/News, Regime.

Deterministic scoring — every point is traceable to a fact in the context.
"""

from agents.base import Agent, AgentOutput, DecisionContext, ask_llm

# Weekly scheduled events that move MCX commodities (IST). The macro agent
# flags them; the conservative risk perspective halves size on flagged days.
# weekday: 0=Mon .. 4=Fri.
SCHEDULED_EVENTS: dict[int, list[tuple[str, str]]] = {
    2: [("EIA crude inventory", "CRUDEOIL")],          # Wednesday
    3: [("EIA natural gas storage", "NATURALGAS")],    # Thursday
}


class TechnicalAnalyst(Agent):
    name = "technical"

    def run(self, ctx: DecisionContext) -> AgentOutput:
        score, notes = 0, []
        want = ctx.signal.action  # BUY / SELL

        if want in ("BUY", "SELL"):
            score += 2
            notes.append(f"{ctx.signal.strategy}: {ctx.signal.reason}")
        if ctx.signal.rr >= 2.0:
            score += 1
            notes.append(f"RR {ctx.signal.rr}")
        regime_dir = "BUY" if ctx.regime.direction == "BULLISH" else "SELL"
        if want == regime_dir:
            score += 1
            notes.append(f"regime direction agrees ({ctx.regime.direction})")
        if ctx.mtf.get("consensus"):
            score += 1
            notes.append(ctx.mtf.get("confidence", "MTF consensus"))
        if ctx.volume_ratio >= 1.5:
            score += 1
            notes.append(f"volume {ctx.volume_ratio:.1f}x average")

        stance = ("BULLISH" if want == "BUY" else "BEARISH") \
            if score >= 4 else "NEUTRAL"
        return AgentOutput(self.name, stance, min(score / 6, 1.0),
                           "; ".join(notes), {"score": score})


class MacroAnalyst(Agent):
    """Covers the blind spot pure-technical bots have: scheduled events.
    Deterministic core = the weekly event calendar; the optional LLM/second
    opinion may only ADD risk flags, never remove them."""

    name = "macro"

    def run(self, ctx: DecisionContext) -> AgentOutput:
        flags = [name for name, sym in SCHEDULED_EVENTS.get(ctx.weekday, [])
                 if sym == ctx.symbol or sym == "ALL"]

        llm_note = ask_llm(
            f"Any major macro risk today for MCX {ctx.symbol}? "
            f"Known scheduled events: {flags or 'none'}.")
        rationale = (f"scheduled events today: {', '.join(flags)}"
                     if flags else "no scheduled high-impact events today")
        if llm_note:
            rationale += f" | LLM: {llm_note}"

        return AgentOutput(self.name,
                           "NEUTRAL",
                           0.5 if flags else 0.8,
                           rationale,
                           {"risk_flags": flags})


class RegimeAnalyst(Agent):
    name = "regime"

    def run(self, ctx: DecisionContext) -> AgentOutput:
        allowed = ctx.regime.can_trade and ctx.mtf.get("consensus", False)
        stance = ("BULLISH" if ctx.regime.direction == "BULLISH"
                  else "BEARISH") if allowed else "VETO"
        return AgentOutput(
            self.name, stance,
            0.9 if allowed else 1.0,
            f"{ctx.regime.regime} (ADX {ctx.regime.adx}, "
            f"{ctx.mtf.get('confidence', 'no MTF data')}); "
            f"trading {'allowed' if allowed else 'NOT allowed'}",
            {"allowed": allowed})
