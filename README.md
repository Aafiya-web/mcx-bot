# MCX Multi-Agent Trading Bot

Autonomous paper-first trading system for 5 MCX commodities (CRUDEOIL,
NATURALGAS, GOLD, SILVER, COPPER) via Angel One SmartAPI, with a
deterministic trading loop, a multi-agent decision layer, hard risk gates,
and a drawdown circuit breaker.

**⚠️ Live trading is OFF by default and refuses to enable itself until every
safety placeholder is configured. Read `HANDOFF.md` before changing that.**

## Quickstart (paper mode, zero credentials needed)

```bash
py -m venv venv
venv\Scripts\pip install -r requirements.txt

venv\Scripts\python.exe scripts\preflight.py       # self-check
venv\Scripts\python.exe -m pytest tests\ -q        # 146 tests
venv\Scripts\python.exe scripts\paper_session.py   # full pipeline demo (mock data)
venv\Scripts\python.exe scripts\run_bot.py         # real-time loop + dashboard
```

Dashboard: http://127.0.0.1:5001 (read-only).

To paper-trade against **live market data**, copy `.env.example` to `.env`
and fill the Angel One credentials — keep `LIVE_TRADING=false`.

## The moving parts

```
feed (mock/live) ─► regime gate ─► strategy per instrument ─► candidate signal
                                                                    │
                              agents: analysts → bull/bear → trader │
                                       → risk team → portfolio mgr ◄┘
                                                    │ approve
              risk gate chain (sizing/correlation/guard — binding) │
                                                                    ▼
             order manager (paper simulator ⇄ live, one interface)
                                                                    ▼
        position monitor: stored stops, market exits, SL-M backstop,
        R-based trailing, slippage audit · squareoff 23:15 IST
```

Key docs: `HANDOFF.md` (architecture, rationale, runbooks — start here),
`TEST_PLAN.md` (validation before live), `CLAUDE.md` + `skills/` (spec).

## Safety model

- `LIVE_TRADING=false` is the single paper→live switch; money limits
  (`INITIAL_CAPITAL`, `MAX_DAILY_LOSS_PCT`, `MAX_DRAWDOWN_PCT`) start unset
  and the system refuses live mode while any are missing.
- Portfolio circuit breaker: configured % below equity peak → close all,
  HALT (persisted); only `scripts/clear_halt.py` resumes.
- Stops are set at entry, only ever tighten, fire as market orders, and are
  backed by a resting SL-M at the exchange.
- Correlation filter: never stacks same-direction risk within energy /
  precious / base-metal clusters.
- Kill switches: Telegram `/pause` and `/halt` (CONFIRM), `systemctl stop`.
