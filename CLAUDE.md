# CLAUDE.md — MCX Multi-Agent Commodity Trading Bot

> Claude Code reads this file automatically. It is the standing project brief.
> Full detail lives in `mcx_master_prompt.md` — read it in full before building.

---

## WHAT THIS PROJECT IS

An autonomous multi-agent trading bot for **MCX** (Multi Commodity Exchange, India)
via **Angel One SmartAPI**, running on an existing Oracle Cloud VM alongside a
separate Solana bot. It trades **5 commodities simultaneously**, each with a
strategy matched to its behavior, under a full autonomous loop with a
multi-agent decision system and a main orchestrator.

## MODEL ROLE (read carefully)

You are **Claude Fable**, available only for this build. Act as the one-time
**brain** that fully designs and creates the bot. After this, **any ordinary
model** (Opus, Sonnet, etc.) must be able to run and maintain it without you.
Therefore: **front-load all intelligence into the artifacts, not yourself.**
Encode market judgment as explicit bounded rules; write everything down. If a
successor would need to be as capable as you to figure something out, you're not
done — push that reasoning into code, config, or docs. The single most important
deliverable is `HANDOFF.md`, the model-agnostic knowledge base.

---

## FILES IN THIS REPO (read these first)

```
mcx_master_prompt.md   ← FULL build spec — read completely before coding
mcx_bot_structure.md   ← reference project layout
.mcp.json              ← MCP config + the 10 skills registry + connectors
skills/                ← 10 SKILL.md files = source of truth for each module:
   mcx-signal-analyzer      (signals, 5-instrument strategy matrix)
   mcx-regime-detector      (ADX/ATR/BB regime gate)
   mcx-risk-manager         (ATR position sizing, limits)
   mcx-correlation-filter   (block stacked energy/metals exposure)
   mcx-portfolio-guard      (drawdown circuit breaker + hard-stop policy)
   mcx-signal-analyzer      (entry/exit + levels)
   mcx-contract-monitor     (expiry tracking + rollover)
   mcx-angel-auth           (TOTP auto-login, token refresh, SEBI)
   mcx-trade-logger         (SQLite, P&L, performance)
   mcx-backtest-runner      (backtest + go/no-go gate)
   mcx-daily-briefing       (morning + evening Telegram reports)
successor_handoff_message.md ← hand to the next model later (not needed now)
```

## THE 5 INSTRUMENTS (trade simultaneously)

| Instrument | Strategy | TF | Cluster |
|---|---|---|---|
| CRUDEOIL | ORB (AM) + Supertrend | 15min | ENERGY |
| NATURALGAS | Momentum breakout (Donchian+vol) | 15min–1H | ENERGY |
| GOLD | 50/200 EMA trend | 1H/4H | PRECIOUS |
| SILVER | Supertrend / mean-revert in range | 15min–1H | PRECIOUS |
| COPPER | Supertrend / EMA trend | 1H | BASE |

Config-driven; mini-contract mode (CRUDEOILM/GOLDM/SILVERM/NATURALGASM) for small
accounts. Each runs independently but shares the correlation filter + circuit breaker.

---

## NON-NEGOTIABLE GUARDRAILS (never violate)

1. **Paper-first, all 5 together.** The system runs in paper mode against live data
   with zero real orders, all 5 instruments simultaneously, logging real metrics
   (win rate, profit factor, drawdown). Prove the whole system here before live.
2. **Backtest gate before live.** 6+ months real MCX data, ₹20/trade + slippage.
   Go/no-go: profit factor > 1.5, max drawdown < 10%, win rate > 50%, 100+ trades.
3. **One-line paper→live switch + kill switch.** `LIVE_TRADING=false` by default.
4. **No hard-coded money values.** `INITIAL_CAPITAL`, `MAX_DAILY_LOSS_PCT`,
   `MAX_DRAWDOWN_PCT` start as `None`. System REFUSES live mode while any are unset.
   Paper mode runs fine without them.
5. **Hard stops, no exceptions.** Stop set at entry; never moved wider, never
   removed; may only tighten. Fire ON TIME: pre-compute stop, market order on
   breach (not limit), resting SL-M backstop at exchange, log intended-vs-filled
   slippage every stop exit. (mcx-portfolio-guard)
6. **Correlation filter mandatory.** Never stack same-direction risk in a cluster
   (energy, precious, base). (mcx-correlation-filter)
7. **Portfolio circuit breaker.** Equity drops configured % from peak → close all,
   HALT, require manual reset. Never auto-resume. (mcx-portfolio-guard)
8. **SEBI compliance.** Static-IP whitelist for orders; OAuth + TOTP 2FA; daily
   session auto-close with 8:50 AM re-login; Algo-ID tag on EVERY order.
9. **Coexist with the Solana bot.** Own virtualenv, own SQLite DB, own systemd
   service, dashboard on port 5001 (Solana uses 5000). Add swap if RAM is tight.

## AUTONOMY DESIGN (hybrid — keeps cost sane)

- **Deterministic layer (always on, cheap):** continuous Python loop — data,
  indicators, regime, position monitoring, stop/TP execution, safety checks. No LLM.
- **Agentic layer (triggered, expensive):** the multi-agent debate fires ONLY on a
  candidate signal, regime shift, risk-limit approach, or scheduled review.
- **Agents:** Technical, Macro/News, Regime analysts → Bull vs Bear debate → Trader
  → Risk team (aggressive/neutral/conservative) → Portfolio Manager (final gate) →
  Reflection/Optimizer (adapts params within bounded, logged limits). A **Main
  Orchestrator** owns state, sequences agents, keeps the decision log, and enforces
  that nothing executes without passing the full risk gate chain:
  `regime → ATR sizing → correlation filter → portfolio guard → execute`.

---

## BUILD ORDER (incremental; keep paper mode runnable at every step)

1. **Foundation:** `config/settings.py` (empty money placeholders, guarded),
   `config/symbols.py` (5 instruments + strategy map), `database/models.py`
   (SQLite schema), `notifications/telegram.py`.
2. **Broker:** `broker/auto_login.py` (TOTP, token refresh), `broker/order_manager.py`
   (paper + live behind ONE interface, Algo-ID tag, SL-M support).
3. **Data:** `data/feed.py`, `data/historical.py`, `data/store.py`.
4. **Indicators + regime:** regime detector (ADX/ATR/BB).
5. **Strategies:** supertrend, orb, ema_trend, momentum_breakout, mean_reversion +
   per-instrument mapping.
6. **Risk:** ATR sizing, correlation filter, portfolio guard (drawdown + hard stops).
7. **Positions:** monitor loop (3s), rollover.
8. **Agents + orchestrator:** all agents + the main orchestrator + decision log.
9. **Engine:** the full autonomous loop running all 5 in paper mode + scheduler
   (market hours, square-off) + kill switch.
10. **Dashboard (5001), daily briefings, systemd service.**
11. **Backtest runner** + the go/no-go report.
12. **HANDOFF.md + README + paper-mode test plan.**

## FRAMEWORK CHOICE

Pick the option easiest to run/maintain on a 1GB VM. Evaluate LangGraph vs. a
lightweight plain-Python orchestrator, recommend one in a short paragraph, default
to the lighter option if close. Don't make me research it.

---

## BEFORE YOU START

Confirm you understand the Fable-as-brain model and the guardrails, ask any
clarifying questions, then begin at Build Order step 1. Do NOT enable live trading.
Research current Angel One SmartAPI + MCX contract specs if it helps correctness —
don't guess on API details or lot sizes.
