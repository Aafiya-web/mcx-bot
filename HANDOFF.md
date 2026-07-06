# HANDOFF.md — MCX Multi-Agent Trading Bot Knowledge Base

> **Audience:** any future model (Opus, Sonnet, anything) or human taking over
> this project. You should never need to re-derive the architecture or the
> strategy reasoning — it is all here and in the code. Assume nothing lives in
> anyone's head. Read this file, then `CLAUDE.md`, then the `skills/` files.

**State as of 2026-07-06:** Build Order steps 1–12 complete. Paper mode runs
end-to-end offline (146 tests green, `scripts/paper_session.py` exercises the
full pipeline). Live trading has NEVER been enabled and must not be until the
runbook at the bottom is completed by the owner.

---

## 1. What this system is

An autonomous trading bot for 5 MCX commodities (CRUDEOIL, NATURALGAS, GOLD,
SILVER, COPPER) via Angel One SmartAPI, with a **hybrid autonomy design**:

- **Deterministic layer** (`core/engine.py`) — a cheap Python loop every ~3s:
  data → indicators → regime → position monitoring → stop/target execution →
  safety checks. No LLM, no API cost.
- **Agentic layer** (`core/orchestrator.py` + `agents/`) — fires ONLY when the
  deterministic layer finds a candidate signal (or on scheduled review):
  analysts → bull/bear debate → trader → risk team → portfolio manager.
  Every stage's output is persisted to the `decision_log` table.

**Critical design rule:** every agent has a deterministic scoring core. LLM
calls (`agents/base.py:ask_llm`, behind `LLM_AGENTS_ENABLED=false`) may only
enrich rationale — they can never approve what the rules rejected. That is why
paper mode costs ₹0 in API fees and the whole test suite runs offline.

## 2. Module map (what lives where, and why)

| Module | Job | Key facts |
|---|---|---|
| `config/settings.py` | All tunables from env | Money values default `None`; `validate_live_config()` refuses live while unset. `LIVE_TRADING=false` is THE switch. |
| `config/symbols.py` | The 5-instrument lineup | Lot sizes, **POINT_VALUES** (₹/point/lot — used for all P&L math), clusters, expiry rules, ATR bands, **MARGIN_PCT_ESTIMATE**, mini-contract toggle. |
| `database/models.py` | SQLite (WAL) | `trades`, `bot_state` (kill switch), `decision_log`, `param_changes`. Add columns via the `_MIGRATIONS` list — never edit DDL of shipped tables. |
| `broker/auto_login.py` | TOTP session | `from SmartApi import SmartConnect` (capitalisation matters, verified v1.4.8). Re-login 08:50 IST (SEBI daily session close). |
| `broker/order_manager.py` | Execution | ONE interface; `get_order_manager()` is the paper/live gate. `LiveExecutor` cannot even be constructed unless live is fully configured. SEBI `ordertag` on every live order. |
| `data/feed.py` | Market data | `MockFeed` = deterministic synthetic data (trend/range segments) so everything runs offline; `LiveFeed` polls Angel. Same interface. |
| `core/indicators.py` | Indicator math | Own implementations (EMA/ATR/ADX/Supertrend/BB/Donchian/VWAP). **No pandas_ta** — it's unmaintained and breaks with modern numpy. |
| `core/regime.py` | The master gate | ADX>25 trending, <20 ranging; BB width for squeeze; per-symbol ATR% bands; strict-majority MTF consensus. Thresholds are adaptable (bounded). |
| `strategies/` | 5 strategies + router | `RegimeGated` wrapper is the only path the engine uses — no trend trades in a range, mean-reversion ONLY in a range. RR<2 demoted to HOLD in `base.make_signal`. |
| `risk/` | The gate chain | `run_gate_chain`: signal → halt → regime → daily limits → ATR sizing → correlation → guard. Nothing executes around it. |
| `positions/monitor.py` | Stops that fire on time | Stored (pre-computed) stop checked vs LTP each tick; MARKET exit on breach; resting SL-M backstop; intended-vs-filled slippage logged on every stop exit. |
| `agents/` | The trading desk | See §1. `reflection.py` owns bounded adaptation. |
| `dashboard/` | Read-only Flask :5001 | No control endpoints on purpose (control = Telegram + host scripts). Solana bot owns :5000. |
| `backtest/engine.py` | The gate before live | Drives the SAME strategy objects as live. Go/no-go: PF>1.5, |DD|<10%, WR>50%, 100+ trades. |

## 3. Non-obvious decisions and WHY (do not silently undo these)

1. **Plain-Python orchestrator, not LangGraph.** 1GB VM shared with the Solana
   bot; the pipeline is a fixed linear sequence; fewer dependencies = a
   beginner can debug it. Revisit only if the agent graph becomes dynamic.
2. **Margin-based caps, not notional.** The skill files capped position value
   at 10% of capital — impossible for leveraged futures (1 crude lot = ₹6L
   notional). Caps now use `MARGIN_PCT_ESTIMATE` (rough, marked VERIFY):
   position ≤30% margin, cluster ≤50% margin of capital.
3. **Sizing refuses instead of rounding up.** The skill's `max(1, lots)` would
   force trades that exceed the 1% risk budget exactly on the most volatile
   instruments. 0 lots = trade refused. GOLD/SILVER standard lots will often
   refuse under ₹10L capital — that is CORRECT; use `USE_MINI_CONTRACTS=true`.
4. **R-multiple trailing, not %-move trailing.** The skill's +10%/+25% ladder
   came from crypto; commodities never move that far intraday. Breakeven at
   +1R, trail 1R behind peak from +1.5R (`positions/monitor.py` constants).
5. **Squareoff at 23:15, entries stop 23:00.** MCX close shifts seasonally
   (23:30/23:55). Early is cheap; overnight positions are not.
6. **Backtest charges ₹20 per side (₹40 round trip)** + slippage both fills,
   and resolves same-bar SL/TP ambiguity as stop-first. Pessimistic by intent.
7. **Halt flag lives in SQLite, not memory.** A restart after a circuit-breaker
   trip stays halted. Only `scripts/clear_halt.py` (human, host-side) clears it.
8. **Dashboard is read-only.** A compromised port 5001 cannot trade.
9. **PAPER_CAPITAL default ₹5,00,000** — smaller capital sizes 0 lots on most
   standard contracts (see #3), which looks like "bot is dead" but is honest.

## 4. Parameters that matter

| Param | Default | Meaning / bounds |
|---|---|---|
| `MAX_RISK_PER_TRADE_PCT` | 1.0 | % of capital risked per trade (stop distance × point value) |
| `MAX_DAILY_LOSS_PCT` | None (paper: 2.0) | Hard daily stop. REQUIRED for live. |
| `MAX_DRAWDOWN_PCT` | None | Circuit breaker % from equity peak. REQUIRED for live; breaker disarmed while unset (paper only). |
| `MIN_REWARD_RISK` | 2.0 | Signals below this become HOLD |
| `ATR_SL_MULT` | 1.5 | Stop distance in ATRs (adaptable 1.0–2.5) |
| `ADX_TRENDING/RANGING` | 25/20 | Regime gate (adaptable 20–30 / 15–22) |
| `AGENT_APPROVAL_MARGIN` | 1.0 | Bull-bear score edge needed (adaptable 0.5–3.0) |
| `PAPER_SLIPPAGE_PCT` | 0.05 | Simulated adverse fill % |

Adaptation: ONLY params in `agents/reflection.py:PARAM_BOUNDS` can change, are
clamped, and land in `param_changes` with old value + reason (rollback = set it
back). Money limits and the live gate are not in the registry — untouchable.

## 5. Failure modes and what to do

| Symptom | Likely cause | Action |
|---|---|---|
| Bot trades nothing for days | Regime RANGING everywhere (correct behavior), or sizing refuses (capital too small for standard lots) | Check `/api/decisions` rationale; consider minis |
| `AG8001` / Invalid token | Session expired | Auto-recovered by `with_auth_retry`; if persistent check TOTP clock sync (`AG8003`) |
| Stop slippage growing in evening reports | Monitor loop lagging (VM overloaded?) | Check `MemoryMax`, swap, tick interval; this was the prior bot's death — treat as P0 |
| HALTED after restart | Circuit breaker tripped earlier | Review trades + decision log, then `scripts/clear_halt.py` |
| No candles in live mode | Exchange holiday, or token stale after rollover | `positions/rollover.py:get_active_contract` re-resolves daily |
| pytest fails on fresh clone | venv missing | `py -m venv venv && venv\Scripts\pip install -r requirements.txt` |

## 6. How to run

```bash
# Paper, offline, no credentials (works on a fresh clone):
venv\Scripts\python.exe scripts\preflight.py       # self-check, must exit 0
venv\Scripts\python.exe -m pytest tests/ -q        # 146 tests
venv\Scripts\python.exe scripts\paper_session.py   # full pipeline in seconds
venv\Scripts\python.exe scripts\run_bot.py         # real-time loop + dashboard :5001

# Paper against LIVE market data: put Angel One creds in .env (LIVE_TRADING
# stays false!) and run scripts\run_bot.py — LiveFeed activates automatically.

# Backtest gate (needs creds for real history):
venv\Scripts\python.exe scripts\backtest.py 180
```

Telegram (optional): set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`. Commands:
`/status /pause /resume /halt`(+CONFIRM). Briefings 07:30 & 23:45 IST.

## 7. Deploy to the Oracle VM (alongside the Solana bot)

1. `git clone` to `/home/ubuntu/mcx-trading-bot`; `python3 -m venv venv`;
   `venv/bin/pip install -r requirements.txt smartapi-python pyotp`.
2. `cp .env.example .env && chmod 600 .env`; fill Angel + Telegram values.
   Leave `LIVE_TRADING=false` and money placeholders unset.
3. `sudo cp scripts/mcx-bot.service /etc/systemd/system/ && sudo systemctl
   daemon-reload && sudo systemctl enable --now mcx-bot`.
4. Check `journalctl -u mcx-bot -f` and `http://127.0.0.1:5001` (SSH tunnel).
5. RAM: unit caps at 350M; add 1–2G swap if the VM thrashes
   (`sudo fallocate -l 2G /swapfile && sudo mkswap /swapfile && sudo swapon`).
6. Deploy flow: change on dev machine → tests green → push to GitHub → pull on
   VM → `sudo systemctl restart mcx-bot`. Review diffs before pulling.

## 8. SEBI compliance checklist (India, effective April 2026)

- **Static IP whitelist:** register the VM's public IP in the SmartAPI app
  settings (smartapi.angelbroking.com → My Apps). Orders from other IPs are
  rejected. Document the IP in `.env` as a comment when done.
- **Algo ID:** obtain the exchange-registered algo tag from Angel One, set
  `ALGO_ID_TAG` in `.env`. The execution layer refuses live orders without it.
- **2FA/TOTP + daily session close:** handled by `broker/auto_login.py`
  (08:50 re-login). Do not extend token reuse past 20h.
- **Order rate:** well under the 10/s retail threshold by design.

## 9. VERIFY-BEFORE-LIVE list (things coded from estimates)

- `LOT_SIZES` / `POINT_VALUES` in `config/symbols.py` vs current MCX contract
  specs (minis especially; check an Angel One contract note).
- `MARGIN_PCT_ESTIMATE` vs the broker margin calculator.
- `producttype` (INTRADAY vs CARRYFORWARD) and `ordertag` field name in
  `broker/order_manager.py:_build_params` vs current SmartAPI docs.
- Exact expiry dates: `parse_expiry` approximates the 20th (rolls early —
  safe); wire the instrument-master expiry when convenient.
- MCX seasonal close time (23:30 vs 23:55) each March/November.

## 10. Paper → Live runbook (the owner executes this, never the bot)

1. Run ≥4 weeks of paper against LIVE data (creds set, `LIVE_TRADING=false`),
   all 5 instruments. Watch evening stop-loss audits: slippage must stay flat.
2. Run `scripts/backtest.py 180` on real history. EVERY instrument you enable
   must pass the gate (PF>1.5, |DD|<10%, WR>50%, 100+ trades). Failing
   instruments stay paper (`ACTIVE_SYMBOLS` env).
3. Complete §8 (IP whitelist, algo tag) and §9 (verify list).
4. Set `INITIAL_CAPITAL`, `MAX_DAILY_LOSS_PCT`, `MAX_DRAWDOWN_PCT` in `.env`.
5. Flip `LIVE_TRADING=true`. Restart. The system re-validates everything and
   refuses to start if anything above was skipped.
6. First live week: minimum size (consider minis), watch `/status` and the
   briefings daily. The kill switches: Telegram `/halt`, or
   `systemctl stop mcx-bot`, or set `MAX_DRAWDOWN_PCT` tight.

## 11. Known gaps / deferred work

- `.mcp.json` + MCP connectors (n8n, Drive, Gmail, Calendar, Netlify) were in
  the original spec but the file was never provided; not required for trading.
- TradingView webhook ingestion (optional signal source) — not built; if added,
  it must enter as a *candidate signal* through the full gate chain.
- Macro agent's calendar is weekly-static (EIA Wed/Thu); a real economic
  calendar feed (Forex Factory RSS) is a good upgrade.
- `daily_summary` table exists but is not yet written by a nightly job; the
  reports compute from `trades` directly.
- Exchange holidays: treated as "no data → no trades", not a calendar.
