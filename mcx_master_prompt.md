# MASTER PROMPT — MCX Multi-Agent Commodity Trading Bot

> Paste this into Claude Code (running on your Oracle VM) to kick off the build.
> Select **Claude Fable** as the model for the architecture/strategy phase.
> Fable designs everything and writes a handoff knowledge base so that
> **Claude Opus** can take over all ongoing coding, deployment, and optimization.

---

## ROLE & MODEL SPLIT — FABLE IS THE BRAIN (ONE-TIME BUILD)

You are Claude Fable, and you are only available during this build. Treat this as
a one-time window to act as the **brain** that fully designs and creates this
MCX (Multi Commodity Exchange, India) autonomous commodity trading bot. After the
bot is created, you will be gone — **any ordinary model** (Opus, Sonnet, or
whatever is available later) must be able to run, maintain, debug, and extend the
system without you. Design for that reality.

This creates one hard requirement: **front-load all the intelligence into the
artifacts, not into yourself.** Every non-obvious decision, every piece of market
judgment, every "why" must be captured in files the system and a future model can
read. Nothing important may live only in your head. If a successor model would
have to be as capable as you to figure something out, you haven't finished the job
— push that reasoning down into code, config, decision rules, or documentation.

**Your job (Fable, now):**
- Design the complete architecture and multi-agent system.
- Implement all strategy logic, risk rules, regime detection, and orchestration.
- Encode your market judgment as explicit, bounded rules the bot follows on its
  own — not as advice that needs a smart model to interpret each time.
- Write `HANDOFF.md`: a model-agnostic knowledge base that teaches ANY future
  model the architecture, the rationale behind every decision, parameter meanings,
  failure modes, and exactly how to run/extend/debug each module. Assume the
  successor is competent but NOT brilliant — spell things out.

**The successor (any model, later):**
- Reads `HANDOFF.md` and the skill files, then handles all ongoing work: building
  remaining pieces, fixing bugs, deploying, and running the bot's own adaptive
  optimization within the safe bounds you defined.
- Should never need to re-derive strategy or architecture from scratch — you
  already did that thinking and wrote it down.

Before writing any code, confirm you understand this and ask me any clarifying
questions (see "ASK FIRST" at the bottom).

Note: if a request gets routed away from Fable mid-session, that's fine — it just
reinforces the rule that `HANDOFF.md` must let any model continue seamlessly.

---

## DEPLOYMENT PHILOSOPHY (NON-NEGOTIABLE ORDER)

1. **Paper-trading first.** Every strategy and the full agent loop must run in a
   simulated / paper mode against live market data, logging what it *would* have
   done, with zero real orders. Prove profitability and stability here first.
2. **Backtest gate (before live).** Every strategy must pass a backtest on 6+
   months of real MCX data, with Angel One's ₹20/trade cost and slippage included,
   using the `mcx-backtest-runner` skill. Hard go/no-go criteria before a strategy
   is eligible for live: profit factor > 1.5, max drawdown < 10%, win rate > 50%,
   and 100+ trades in the sample. A strategy that fails does not go live — period.
3. **Auto-trading last.** Only after paper results are validated AND the backtest
   gate passes (and I explicitly flip a single, obvious config flag like
   `LIVE_TRADING=false → true`) does the system place real orders. Design so the
   paper→live switch is one line, and so live mode has an always-available kill switch.

---

## AUTONOMY MODEL — FULL LOOP (with a hybrid cost-control design)

The system runs a **full autonomous loop during MCX market hours** (9:00 AM –
11:30 PM IST, Mon–Fri). But do NOT run expensive LLM agents on every tick — that
is financially ruinous and slow. Use the 2026 best-practice **hybrid pattern**:

- **Deterministic layer (always on, cheap):** a continuous Python loop handles
  live data, indicator math, regime detection, position monitoring, stop-loss /
  take-profit execution, and safety checks. Runs every few seconds. No LLM calls.
- **Agentic layer (triggered, expensive):** the multi-agent LLM debate fires
  ONLY when the deterministic layer flags something worth deliberating —
  a candidate signal, a regime shift, a risk-limit approach, or a scheduled
  periodic review. This keeps the "full loop" running full-time without paying
  for an LLM every 60 seconds.

Make the trigger thresholds configurable so I can tune cost vs. responsiveness.

---

## MULTI-AGENT SYSTEM + MAIN ORCHESTRATOR

Build a multi-agent architecture inspired by how a real commodity trading desk
works (modeled on the TradingAgents research pattern), adapted for MCX. Include
a **Main Orchestrator** that owns state, sequences the agents, resolves their
disagreements, and enforces that nothing reaches execution without passing risk.

**Main Orchestrator (the conductor):**
- Maintains system state and the shared "decision log" memory.
- Decides when to invoke the agentic layer vs. stay deterministic.
- Routes data between agents, aggregates their outputs, and makes the final
  gated call (analysts → researchers → risk → portfolio manager → execution).
- Persists every decision + rationale to an append-only memory file so the
  system can learn from past calls.

**Agent roster (each with its own tools, constraints, and structured output):**

1. **Technical Analyst Agent** — reads price action, Supertrend, ADX, ORB, ATR,
   multi-timeframe (15min + 1H) structure; outputs a technical view.
2. **Macro / News Analyst Agent** — interprets scheduled and breaking events that
   move commodities (OPEC, EIA inventories, Fed decisions, CPI, DXY, geopolitics).
   This is the blind spot pure-technical bots have — cover it here.
3. **Regime Analyst Agent** — classifies market as trending / ranging / volatile
   and declares whether trading is even allowed right now.
4. **Bull Researcher & Bear Researcher** — debate the analysts' findings
   dialectically (one argues for the trade, one against) to stress-test the thesis
   and surface hidden risk before any capital is considered.
5. **Trader Agent** — consolidates the debate into a concrete proposal: direction,
   entry, stop, target, timing, and size logic.
6. **Risk Management Team** — three sub-perspectives (aggressive / neutral /
   conservative) that pressure-test the proposal against volatility, liquidity,
   exposure, contract expiry, and the safety ceilings. Enforces the full gate
   chain: regime OK → ATR position sizing → correlation filter → portfolio guard
   (drawdown breaker + hard-stop policy). Can veto or resize any proposal.
7. **Portfolio Manager Agent** — the final gate. Approves or rejects. Only an
   approved order proceeds to the (paper or live) execution layer.
8. **Reflection / Optimizer Agent** — runs after trades close and on a schedule;
   reviews outcomes vs. predictions, updates the memory/playbook, and proposes
   parameter or strategy adjustments. This is what makes the bot **adapt to the
   market over time** rather than staying static.

All agents communicate via structured outputs (clear machine-readable fields)
plus short natural-language rationale. Keep the protocol clean and auditable.

---

## ADAPTATION MANDATE (KEEP CHANGING WITH THE MARKET)

The system must not be a fixed rule set. Build in continuous adaptation:
- The Reflection/Optimizer Agent periodically reviews recent performance
  (win rate, profit factor, drawdown, which regimes/strategies worked) and
  adjusts tunable parameters within safe, bounded ranges.
- Log every change it makes, with reasoning, to a versioned changelog so I can
  see *what* changed, *when*, and *why* — and roll back if needed.
- Never let self-optimization override the hard safety ceilings or the paper→live
  gate. Adaptation happens inside guardrails, never around them.
- Prefer bounded in-context/parameter adaptation first; only propose deeper
  structural changes to me for approval, never silently.

---

## MCX DOMAIN PARAMETERS (USE THESE)

- **Broker / API:** Angel One SmartAPI (free, full MCX support, TOTP login).
  Design the broker layer behind a clean interface so it can be swapped for
  Zerodha Kite / Fyers / Upstox / Dhan later without rewriting strategy code.
- **Symbols (start focused, expand later):** begin with **CRUDEOIL** only, then
  add GOLD, SILVER, NATURALGAS once proven. Make the active set config-driven.
- **Timeframes:** 15-minute primary, 1-hour for higher-timeframe bias.
- **Strategies:** Supertrend (10, 3) as the trend engine; Opening Range Breakout
  (30-min) for the morning session; EMA crossover and VWAP mean-reversion as
  secondaries (VWAP for ranging/sideways sessions). **ADX regime filter is the
  master gate** — no trend strategy runs in a ranging market, and mean-reversion
  only runs when the regime is explicitly ranging.
- **Trade discipline:** minimum 2:1 reward-to-risk; ATR-based stops (never fixed);
  volume confirmation on breakouts; auto square-off before the 11:30 PM close;
  contract-expiry monitoring with automatic rollover.
- **Position sizing (ATR-driven):** the stop-loss distance determines the position
  size, never the other way around. A quiet commodity gets a larger position, a
  volatile one gets a smaller position, so the rupee risk stays constant across
  instruments regardless of volatility.
- **Correlation filter (mandatory):** never stack same-direction risk within a
  correlated cluster (energy: CRUDEOIL+NATURALGAS; precious metals: GOLD+SILVER;
  base metals: COPPER+ZINC+ALUMINIUM). If already long the cluster, block or
  resize a second correlated long. This prevents secretly doubling real exposure
  while it looks like diversification. See the `mcx-correlation-filter` skill.
- **Hard stops, no exceptions:** every trade gets a predetermined stop set the
  moment it opens. The bot NEVER moves a stop further from price, NEVER "gives it
  room," NEVER removes a stop. Stops may only tighten (breakeven / trailing).
  Additionally — and this is critical given a prior bot's late-stop history —
  ensure stops fire ON TIME: pre-compute the stop price at entry, check it every
  few seconds against LTP, fire a MARKET (not limit) order on breach, place a
  resting SL-M order at the exchange as a backstop, and log intended-vs-filled
  slippage on every stop exit. See the `mcx-portfolio-guard` skill.
- **Portfolio drawdown circuit breaker:** track equity peak; if total equity falls
  a configured % from its peak, close ALL positions and HALT trading until I
  manually review and clear the halt. Never auto-resume. This is the catastrophic-
  loss circuit breaker. Leave the % as an empty placeholder (see capital rule
  below). See the `mcx-portfolio-guard` skill.
- **Per-instrument strategy matching:** different commodities behave differently,
  so match the strategy to the instrument's character — e.g. commodities trend in
  cleaner waves on higher timeframes, so favor trend-following (Supertrend / EMA)
  on 4H/1H for smoother contracts like GOLD, and keep faster intraday setups (ORB)
  for the liquid, news-driven CRUDEOIL session. Make this mapping config-driven.
- **Capital & loss ceilings:** **DO NOT hard-code any starting capital, daily-loss
  limit, or max-drawdown %.** Leave them as clearly-marked, empty config
  placeholders (e.g. `INITIAL_CAPITAL = None`, `MAX_DAILY_LOSS_PCT = None`,
  `MAX_DRAWDOWN_PCT = None`) with comments explaining they must be set before live
  trading. The system must refuse to enter LIVE mode while any are unset, but
  should run fine in paper mode.
- **Twice-daily hands-off briefings:** generate a pre-market morning briefing
  (~07:30 IST: open positions + unrealized P&L, yesterday's result, 7-day win
  rate, market context like EIA/OPEC/Fed events, risk flags) and a post-close
  evening report (~23:45 IST: today's trades, P&L, best/worst, and a mandatory
  stop-loss audit checking intended-vs-filled price). Both under 200 words, sent
  to Telegram. This makes the system hands-off — I read two messages a day. See
  the `mcx-daily-briefing` skill. These can run as Claude Cowork routines.

---

## SEBI COMPLIANCE (MANDATORY, INDIA, EFFECTIVE APRIL 2026)

Bake these in from the start:
- **Static IP whitelisting** — orders must originate from the Oracle VM's
  registered static IP; document the whitelist step in `HANDOFF.md`.
- **OAuth + mandatory 2FA (TOTP)** per session; **sessions auto-close daily** —
  build the 8:50 AM auto-login/refresh accordingly.
- **Algo-ID / tag on every order** — every order must carry the unique algo tag;
  make it a required field the execution layer cannot omit.
- Stay under the 10-orders-per-second retail threshold (trivially true here).

---

## INFRASTRUCTURE

- **Host:** existing Oracle Cloud free-tier VM (`~1GB RAM`) — must coexist with a
  separate Solana bot already running. Use its own virtualenv, its own SQLite DB,
  its own systemd service, and a distinct dashboard port (Solana uses 5000 →
  MCX uses 5001). Add swap space if memory is tight.
- **Stack:** Python, SQLite, Flask dashboard, Telegram notifications, systemd with
  auto-restart. Reuse the proven patterns from the Solana bot where sensible.
- **Existing assets to wire in:** 10 pre-built MCX skills already exist as SKILL.md
  files (`mcx-signal-analyzer`, `mcx-risk-manager`, `mcx-trade-logger`,
  `mcx-backtest-runner`, `mcx-contract-monitor`, `mcx-regime-detector`,
  `mcx-angel-auth`, `mcx-correlation-filter`, `mcx-portfolio-guard`,
  `mcx-daily-briefing`) plus a `.mcp.json`. Use them as the source of truth for
  each module's logic and connect the listed MCP services (n8n, Google Drive,
  Gmail, Google Calendar, Netlify).
- **Optional intelligence:** an OpenAI/ChatGPT API call MAY be used inside the
  Macro/News Analyst as an independent second opinion — keep it optional and
  behind a config flag, not a hard dependency.
- **TradingView (optional signal source):** support inbound TradingView webhook
  alerts (Pine Script) as an *additional* candidate-signal trigger into the
  deterministic layer. Webhooks must still pass through the full agent + risk
  gate before execution — never let an external webhook place an order directly.
  Remember SEBI static-IP rules apply to any webhook→order path.
- **GitHub (code + deployment):** keep the project in a GitHub repo; use it for
  version control, reviewing diffs before deploying to the VM, and auto-filing an
  issue when the bot hits a critical error. Document the deploy flow in HANDOFF.md.

---

## ORCHESTRATION FRAMEWORK — PICK THE EASIEST TO OPERATE

I want the option that is **easiest for me to run and maintain**, not the fanciest.
Evaluate LangGraph (stateful, production-standard for multi-agent, but heavier and
adds dependencies on a 1GB VM) vs. a lightweight plain-Python orchestrator (fewer
moving parts, easier for a beginner to debug, but you build the state handling).
Recommend one, explain the trade-off in one short paragraph, default to the
lighter option if they're close, then proceed. Don't make me research it.

---

## PRODUCTION ARCHITECTURE (build all five layers, per 2026 best practice)

1. **Reasoning layer** — the LLM agents above.
2. **Orchestration layer** — the Main Orchestrator loop: state, transitions,
   error handling, retries, progress persistence.
3. **Memory layer** — an always-on decision log + performance history + the
   adaptive playbook the Reflection agent updates.
4. **Tools layer** — broker API, data feed, indicators, DB, Telegram, MCP services.
5. **Execution layer** — paper simulator and live executor behind one interface,
   with the paper→live gate and kill switch.

---

## DELIVERABLES (what Fable must produce before it's gone)

1. `HANDOFF.md` — the model-agnostic knowledge base that lets ANY future model run
   and maintain everything (architecture, rationale, params, failure modes, how to
   extend each part, the paper→live runbook). This is the single most important
   deliverable — if it's incomplete, the bot dies when you leave.
2. Full project scaffold with every module stubbed and the core logic implemented.
3. The Main Orchestrator + all agents wired together, runnable in paper mode.
4. Your market judgment encoded as explicit, bounded rules/config the bot executes
   on its own — not as prose that needs a smart model to interpret live.
5. Config files with the capital/daily-loss placeholders left empty and guarded.
6. A short `README` quickstart and the systemd service file.
7. A test plan: how to validate paper mode and pass the backtest gate before live.

---

## ASK FIRST (before building)

Ask me anything you need, but especially:
- Anything ambiguous about the agent design, triggers, or the paper→live gate.
- What data/history you need from the existing Solana bot to reuse its patterns.
- Whether to fetch the current Angel One SmartAPI + MCX contract specs before
  coding (do the research if it helps correctness — don't guess on API details
  or lot sizes).

Once you've confirmed the plan with me, begin Phase A. Build incrementally,
test each module as you go, and keep everything runnable in paper mode at every
step.
