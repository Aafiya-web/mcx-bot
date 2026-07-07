# TEST PLAN — validating paper mode and passing the gate to live

Three stages. Do not skip stages; do not reorder them.

## Stage A — machinery validation (offline, done continuously)

| Check | Command | Pass criteria |
|---|---|---|
| Self-check | `scripts/preflight.py` | exit 0 on fresh clone, PAPER mode |
| Unit + integration | `pytest tests/ -q` | all green (146 as of handoff) |
| End-to-end pipeline | `scripts/paper_session.py` | exit 0; ≥1 candidate; 0 open at end; decision_log populated |
| Live-mode refusal | `pytest tests/test_step2_broker.py -q` | LiveExecutor refuses unconfigured/paper |

Run stage A after every code change. It requires no credentials and no market.

## Stage B — paper against live market data (≥ 4 weeks)

**Prerequisite (met 2026-07-07):** the Stage-B hardening items (HANDOFF
§3b L1/L2/L6/L7/L8) are implemented and tested. During the first live-data
days, additionally watch: scan cadence (log shows one scan per 15 min, no
AB1010 errors), the 09:05 contract-maintenance job, and that daily limits
reset at the new session (`bot_state.daily_tracker`).

Setup: Angel One credentials in `.env`, `LIVE_TRADING=false`, Telegram
configured, bot running via systemd (or `scripts/run_bot.py`).

Weekly review checklist:
1. **Stop-loss audit (evening reports):** average slippage flat and < 0.3%.
   Rising slippage = the prior bot's failure mode; stop and investigate.
2. **Decision quality:** sample `/api/decisions` — rejections should cite
   sensible reasons (regime, correlation, debate). Approvals should match
   what you'd expect from the strategy tables in `skills/mcx-signal-analyzer`.
3. **Discipline invariants (query `trades`):**
   - no trade with RR < 2 at entry;
   - no stop further from price than at entry (compare vs `initial_risk`);
   - no same-direction pair within a cluster open simultaneously;
   - all positions closed by 23:15 IST daily.
4. **Metrics accumulating toward the gate:** win rate, PF, drawdown from
   `/api/performance` — logged weekly in a notebook/sheet.
5. **Ops:** no unexplained restarts (`journalctl -u mcx-bot`), RAM inside
   350M, briefings arriving 07:30/23:45.

Exit criteria for stage B: 4+ clean weeks, zero discipline violations, zero
missed stops, stable ops.

## Stage C — the backtest gate (before ANY live flag flip)

```
venv\Scripts\python.exe scripts\backtest.py 180
```

Per instrument, ALL must hold on ≥6 months of real MCX data with costs:
- profit factor > 1.5
- max drawdown < 10%
- win rate > 50%
- ≥ 100 trades

Instruments that fail stay out of `ACTIVE_SYMBOLS` for live. No exceptions,
no "it almost passed". Then follow the Paper → Live runbook in
`HANDOFF.md §10`.
