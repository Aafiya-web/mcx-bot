# HANDOFF.md — MCX Multi-Agent Trading Bot Knowledge Base

> **Audience:** any future model (Opus, Sonnet, anything) or human taking over
> this project. You should never need to re-derive the architecture or the
> strategy reasoning — it is all here and in the code. Assume nothing lives in
> anyone's head. Read this file, then `CLAUDE.md`, then the `skills/` files.

**State as of 2026-07-07:** Build Order steps 1–12 complete, plus the live
economic-calendar feed (step 13). Paper mode runs end-to-end offline (159
tests green, `scripts/paper_session.py` exercises the full pipeline). Live
trading has NEVER been enabled and must not be until the runbook at the
bottom is completed by the owner.

**Before you change ANY code, read §3b (landmines) and §3c (per-module
notes).** Several things in this system look like bugs but are deliberate,
and several deliberate scope cuts will bite if you run modes that were
never exercised (§12 must-fix list). The original builder audited its own
work adversarially on 2026-07-07; §3b is that audit.

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

## 3b. LANDMINES — where money gets lost, or a "fix" makes things worse

Read this before changing ANY behavior. Tags: 💸 = can lose money,
🧨 = breaks silently, 🧠 = looks wrong but is deliberate — do not "fix".
Entries marked **[FIXED 2026-07-07]** are implemented and tested
(`tests/test_step14_hardening.py`); their text is kept so the reasoning
isn't regressed away.

- **L1 💸 [FIXED 2026-07-07] The tick loop is single-threaded — entry
  scans must stay off the per-tick path.** `Engine.tick()` does stop
  checks AND candle fetches AND agent evaluation on one thread. Scanning
  every tick with `LiveFeed` would mean ~20 HTTP calls per 3s tick —
  Angel rate limits (AB1010) and stop checks delayed by seconds: the
  exact late-stop failure this project exists to prevent. **Fixed:** the
  engine scans once per 15-min bucket (`_last_scan_bucket`); ticks
  between bar closes do only LTP-level stop/equity work. If evening
  slippage audits ever degrade, the next escalation is `monitor.check()`
  on its own 1s thread (it is self-contained by design). DO NOT regress
  this by scanning per tick, and DO NOT "reduce load" by increasing the
  tick interval — that trades stop latency for comfort.
- **L2 💸 [FIXED 2026-07-07] Fetch depth must be interval-aware.**
  `LiveFeed.BARS_PER_DAY` translates lookback bars into fetch days per
  interval (15-min ≈ 58/session, 1H ≈ 14.5, +40% holiday margin). The
  original flat ~30-bars/day guess starved `ONE_HOUR` lookbacks below the
  EMA-200 warmup, silently muting GOLD's strategy in live mode. If you
  add an interval, add its row.
- **L3 💸 [FIXED 2026-07-07] Hourly strategies need a bigger backtest
  window.** `backtest.engine.lookback_for(timeframe)` gives 900 15-min
  bars to `ONE_HOUR` strategies (≈225 hourly bars, clearing EMA-200
  warmup) and 250 to intraday ones; `scripts/backtest.py` applies it per
  instrument. With the old flat 250, GOLD printed "No trades generated" —
  a window artifact that looked like a broken strategy.
- **L4 💸 [FIXED in code, VERIFY on broker] SL-M backstop double-fire
  race.** On any close the monitor now cancels the resting backstop
  FIRST; if the cancel is rejected (the exchange already executed it),
  it reconciles via `OrderManager.get_fill()` and does NOT fire its own
  market exit — the backstop's fill IS the exit. Without this, a fast
  market could fill both and flip the position. The paper race is tested
  (`test_backstop_fill_is_reconciled_not_doubled`); the LIVE half depends
  on Angel order-book field names (`LiveExecutor.get_fill`) — still in
  §9's verify list. Never reorder _close back to exit-then-cancel.
- **L5 🧨 `PaperExecutor.process_pending()` is never called by the engine
  — deliberately.** The monitor fires stops itself and cancels the
  backstop. If you see "stuck PENDING orders" and wire process_pending
  into the loop, every stop will exit TWICE (monitor exit + phantom
  backstop fill). It exists for tests and future multi-process designs.
- **L6 🧨 [FIXED 2026-07-07] Daily limits persist and reset at day
  boundaries.** The engine builds `DailyLimitTracker(persist=True)`:
  counters live in `bot_state` (key `daily_tracker`), so a crash after
  heavy losses can no longer re-arm the full daily budget; `tick()` calls
  `roll_date()` so counters reset at the IST date change instead of
  accumulating forever. Plain in-memory behavior remains the default for
  ad-hoc/test construction — don't flip that default.
- **L7 🧨 [FIXED 2026-07-07] The circuit breaker's equity peak persists
  like its halt flag** (`bot_state.equity_peak`), so a slow bleed
  punctuated by restarts still trips the breaker. Cleared ONLY by
  `manual_reset()` / `scripts/clear_halt.py` — which now also resets the
  peak so the breaker re-arms from post-review equity.
- **L8 🧨 [FIXED in code, VERIFY on broker] Rollover and expiry are
  wired.** `run_bot.ContractBook` is the ONLY place that knows contract
  months: `LiveExecutor` gets `contract_fn` (base name → active
  tradingsymbol+token — live orders now carry the real contract symbol),
  the engine gets `expiry_fn` (refreshed daily in `tick()`, feeds
  `ctx.days_to_expiry`, so the risk team's expiry veto/halving actually
  fires), and a daily 09:05 job rolls positions:
  close-in-old-month → `switch_fn` re-points the book/tokens → reopen
  same BASE symbol in the new month. Positions stay keyed by base names
  everywhere — do not store contract months in the trades table. The
  live path (searchScrip fields, real fills) is in §9's verify list.
- **L9 🧨 Parameter adaptation does not survive restarts — by design,
  but the logs will confuse you.** `apply_param` mutates module globals;
  a restart reverts to defaults while `param_changes` says otherwise.
  Restart-as-reset-to-known-good is intentional. If persistence is ever
  wanted: replay `param_changes` through `apply_param` at startup (it
  re-clamps to bounds) — never bypass the bounds.
- **L10 🧠 All DB timestamps are UTC** (SQLite `CURRENT_TIMESTAMP`).
  Day-bucketed queries (`DATE(exit_time)=DATE('now')`) are correct ONLY
  because the whole MCX session (09:00–23:30 IST = 03:30–18:00 UTC) fits
  inside one UTC day. Any overnight feature, or storing IST strings,
  silently breaks daily P&L, briefings, and the trades/day counter.
- **L11 🧠 Finnhub event times are ASSUMED to be UTC** — the docs were
  unverifiable at build time (JS-only page). If they are actually
  US-Eastern, every blackout window is shifted 4–5 hours. Verify with the
  first real key: EIA crude inventory must show ~20:00 IST on a Wednesday
  (`ts_ist()`). The static fallback hardcodes 14:30 UTC (the EDT release
  time); in US winter the real release is 15:30 UTC — a known ±1h drift
  in the fallback only, accepted.
- **L12 🧠 Entry price vs signal levels mismatch is intentional.** Trades
  log the slipped FILL as `entry_price`, while stop/target come from the
  signal bar's close-based ATR structure; realized RR is slightly worse
  than the gate's RR. DO NOT "fix" by recomputing the stop from the fill:
  the stop is a structural level, not an offset from wherever we happened
  to fill; recomputing widens stops after bad fills — exactly backwards.
- **L13 🧠 The backtest is pessimistic ON PURPOSE** (same-bar SL/TP →
  stop-first, ₹40 round trip, slippage both fills, day-end squareoff).
  If results improve after touching `backtest/engine.py`, first suspect
  you broke the pessimism, not that you found alpha.
- **L14 🧠 MockFeed profitability is meaningless.** `paper_session.py`
  proves the plumbing; its win rate is noise from a synthetic random
  walk. Never quote it as evidence for the go/no-go gate.
- **L15 🧠 The correlation filter allows OPPOSITE-direction positions in
  the same cluster on purpose** (long CRUDEOIL + short NATURALGAS is a
  spread, not doubled exposure). Only same-direction stacking is blocked.
  Don't "tighten" it to block both without understanding that.

## 3c. Per-module maintenance notes (invariants / debugging / extending)

**config/settings.py** — Invariant: money values are `None` until a human
sets them; `validate_live_config()` reads module globals (not env) so tests
and runtime overrides agree. Extend by adding an env-typed constant + (if
live-critical) a line in `validate_live_config`. Never give a money value a
default.

**config/symbols.py** — One source of truth. P&L and sizing use
`POINT_VALUES`, never `LOT_SIZES` (GOLD: lot 1kg but quotes per 10g → point
value 100 ≠ lot size 1000). New instrument = add to all six dicts + a
cluster, or tests in `test_step1` fail and tell you what's missing.

**database/models.py** — Schema changes ONLY via the `_MIGRATIONS` list
(idempotent ALTERs); never edit shipped DDL — existing DBs won't get the
change. Every function takes `db_path` so tests isolate; keep that on
anything you add. WAL means a reader (dashboard) never blocks the writer.

**broker/** — `get_order_manager()` is the only legal way to obtain an
executor. `SmartApi` imports are lazy INSIDE functions — keep it that way
or paper mode grows a hard dependency. Debug live auth with the error
table in §5 / the mcx-angel-auth skill.

**data/feed.py** — Both feeds honor one contract: `get_ltp`,
`get_candles(symbol, interval, lookback)` → OHLCV indexed by timestamp.
Anything new (websocket feed, another broker) implements that contract and
nothing downstream changes. MockFeed is deterministic per seed — bug
reproductions should quote the seed. See L2 before trusting LiveFeed
lookbacks.

**data/economic_calendar.py** — New source = implement
`CalendarProvider.fetch()` returning `EconEvent`s in UTC and pass it to
`EconomicCalendar(provider=...)`. The regex map is the relevance filter;
extending coverage means extending `EVENT_SYMBOL_MAP`, not trusting
provider impact grades. Debug: `bot_state.econ_cal_cache` shows exactly
what the bot believes today.

**core/indicators.py** — Pure functions, unit-tested against hand-computed
values. If an indicator "looks wrong", write the arithmetic for 3 bars by
hand first; Wilder smoothing (ATR/ADX) intentionally differs from plain
EMA. Donchian is shifted one bar on purpose — unshifted channels can never
break out (test_donchian explains).

**core/regime.py** — Thresholds are module globals BECAUSE the reflection
agent mutates them (bounded). Don't convert to constants/frozen config
without reworking `agents/reflection.py:PARAM_BOUNDS` targets.

**strategies/** — Discipline lives in `base.make_signal` (RR gate) and
`router.RegimeGated` (regime gate). A new strategy: subclass `Strategy`,
return via `make_signal`, register in `router.get_strategy`, map in
`config/symbols.py` — and it inherits every guardrail. Never let the
engine call an unwrapped strategy.

**risk/** — The chain order in `run_gate_chain` is meaningful (cheap/fatal
checks first). New checks follow the `check(name, ok, detail)` pattern so
they appear in every decision-log audit. `GateResult.checks` is the
debugging tool: it tells you exactly which gate refused and why.

**positions/monitor.py** — Invariants: stop compared against the STORED
number; exits are MARKET orders; stops only tighten; backstop cancelled on
every close. The R-ladder constants at the top are the only tuning knobs.
Slippage prints as `STOP AUDIT` log lines and in the evening report — that
is the module's health metric.

**agents/ + core/orchestrator.py** — Deterministic scores are the
decision; LLM output is annotation. Adding an agent: subclass `Agent`,
return `AgentOutput`, insert into `Orchestrator.evaluate`'s sequence, and
it is automatically decision-logged. The PM must remain the only path to
execution.

**notifications/** — `send_message` never raises; keep that property, the
engine relies on it. The command poller trusts only `TELEGRAM_CHAT_ID`;
`/halt` must never become clearable remotely.

**dashboard/** — Read-only by design; adding a control endpoint would give
port 5001 trading authority. Add views as new `/api/*` routes reading
SQLite directly.

**backtest/** — Uses the live strategy objects deliberately; if you fork
backtest-only strategy logic, the gate stops validating what actually
runs. See L3/L13.

**scripts/ + tests/** — `preflight.py` is meant to accrete one line per
subsystem; when you add a module, add its line. Tests are numbered by
build step; new features get a new `test_stepN_*.py`, regressions get a
test in the step they belong to.

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
| `EVENT_BLACKOUT_MINUTES` | 120 | No new entries on an instrument while one of its high-impact events is this close ahead |
| `FINNHUB_API_KEY` | empty | Economic calendar source; empty = static weekly fallback |

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
- Finnhub event timezone is UTC (landmine L11): confirm EIA crude shows
  ~20:00 IST on a Wednesday via `ts_ist()` with the real key.
- L4 broker half: `LiveExecutor.get_fill` order-book field names
  (orderid/status/averageprice/filledshares) against current SmartAPI
  docs, then prove the cancel-reject → reconcile path on ONE minimum-size
  live order before trusting it.
- L8 broker half: `searchScrip` response fields used by
  `get_active_contract`/`get_next_contract`, and one real rollover
  walked through manually the first time it triggers.

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

## 11. Economic calendar feed (`data/economic_calendar.py`)

The macro agent and the entry-blackout gate read a real calendar, not a
static table (added 2026-07-07):

- **Primary provider:** Finnhub `GET /api/v1/calendar/economic` (set
  `FINNHUB_API_KEY` in `.env`; free signup at finnhub.io, but the calendar
  endpoint may be premium-gated — a 401/403 just triggers the fallback).
- **Fallback:** `StaticWeeklyProvider` — the original EIA Wed/Thu table,
  regenerated as dated events at 14:30 UTC (20:00 IST). Used whenever the
  primary fails for ANY reason. The source in use is logged
  (`WARNING ... using 'static-weekly' fallback`), cached, and printed in the
  morning briefing's MARKET CONTEXT line — a silent fallback is impossible.
- **Relevance filter:** the regex map `EVENT_SYMBOL_MAP` (Fed/FOMC → all 5,
  US CPI → metals+crude, NFP, EIA crude/gas, OPEC) IS the high-impact
  filter; provider impact grades are recorded but not trusted.
- **Enforcement:** events within `EVENT_BLACKOUT_MINUTES` (default 2h) of
  `now` block NEW entries on affected instruments via the gate chain's
  `event_blackout` check — auditable in every decision-log row, impossible
  to bypass. Events later the same day (outside the window) still halve
  size via the conservative risk perspective, as before.
- **Cache:** one JSON blob per day in `bot_state` (`econ_cal_cache`) — one
  API call per day, refreshed at the 07:30 briefing or first access after
  midnight. Swap providers by implementing `CalendarProvider.fetch()` and
  passing it to `EconomicCalendar(provider=...)`.
- **Timezone:** providers deliver UTC; conversion uses fixed +05:30 (India
  has no DST). The host clock must be IST — the systemd unit now pins
  `Environment=TZ=Asia/Kolkata` (Oracle VMs default to UTC).
- **Limitations:** OPEC meetings are caught only if the provider carries
  them (irregular dates, not in the fallback); geopolitical headlines are
  out of scope for a calendar API — the optional LLM second opinion in the
  macro agent is the hook for that.

## 12. Known gaps / deferred work

**Update 2026-07-07:** the former MUST-FIX-before-Stage-B block
(L1/L2/L3/L6/L7/L8) is implemented and tested — see the [FIXED] tags in
§3b and `tests/test_step14_hardening.py`. What remains before Stage B is
credentials-only; what remains before LIVE is the §9 verify list, notably
the broker-side halves of L4 (order-book reconciliation fields) and L8
(searchScrip contract resolution), which cannot be proven offline.

- `.mcp.json` + MCP connectors (n8n, Drive, Gmail, Calendar, Netlify) were in
  the original spec but the file was never provided; not required for trading.
- TradingView webhook ingestion (optional signal source) — not built; if added,
  it must enter as a *candidate signal* through the full gate chain.
- `daily_summary` table exists but is not yet written by a nightly job; the
  reports compute from `trades` directly.
- Exchange holidays: treated as "no data → no trades", not a calendar.
- OPEC meeting dates and geopolitical events: see §11 limitations.
