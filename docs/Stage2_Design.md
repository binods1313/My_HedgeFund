# Stage 2 Design — Signal to Paper Execution

**Status: DESIGN ONLY. No code until approved.** PAPER only, no `--live`,
brain parked, `execution_mode: approve_first`, magic-234000 untouched.

Scope: a deterministic signal fires on a closed bar, waits for a human
decision, and only then reaches the PAPER executor. Every step is journaled
under one decision ID so a signal can be traced from bar to closed position.

---

## 1. Signal generation (deterministic, closed bars)

**Where:** `engine/scan_cycle.py` performs terminal/account and per-symbol
filters and determines which symbols have a newly closed M15 bar. Stage 2
requests history only for that eligible subset and evaluates only after those
filters. It must not add the nine tickless FULL crosses; the approved
16-symbol whitelist and 60-second scan interval remain unchanged.

**Data contract:** use M15 bars, consistent with the existing dry-run default
and approval-flow context. On the gateway owner thread, make one batched
history-read call per scan cycle, passing exactly the symbols with a newly
closed M15 bar; do not read history for unchanged symbols, repeat per-symbol
reads inside the cycle, or open a second MT5 session. Request up to 300 fully
closed M15 bars per eligible symbol. Never pass the currently forming bar to
the strategy. A fake-gateway test must assert that the number of per-symbol
history results equals the number of newly closed bars, and that a multi-cycle
sequence does not read `symbols × cycles` when bars did not close.

`engine/strategy.py::evaluate()` calls `validate_ohlcv(..., min_rows=30)`, but
that is not the effective history floor: `ema_ribbon()` uses a 200-bar slow EMA
and a five-bar slope window, and explicitly requires
`200 + 5 + 1 = 206` rows. The Stage 2 caller therefore requires at least 206
valid, closed rows before calling `evaluate`; 300 is the fetch target, not a
reason to weaken that floor. The structure blocks also need chronological,
valid OHLC data; the 20-bar sweep lookback, confirmed pivots, prior-day
extremes, three-candle gaps, swing leg and session VWAP are evaluated from the
same frame.

After restart, if the terminal returns fewer than 206 closed bars, do not pad
history, use a forming bar, or record a false "no signal". Write a visible
`INSUFFICIENT_HISTORY` scan result for that symbol, skip strategy evaluation,
and retry on a later cycle. A malformed frame or strategy exception is a
visible per-symbol error; the rest of the cycle continues.

For each eligible symbol with a new M15 close, call
`engine/strategy.py::evaluate(symbol, df)`. Only an actionable signal whose
required blocks agree proceeds. Conflicts reject rather than down-weight.

**Decision ID:** `decision_id = uuid5(NAMESPACE, f"{symbol}|{strategy}|{bar_close_ts}")`.
For the deterministic strategy, `strategy` is `confluence`; the identity is
stable across retries. The same ID is stored on the signal, approval/rejection
command, order, position and close lifecycle records.

**Schema v4 columns:** v3 `signals` is `id, symbol, direction, score, bar_ts,
ts` with `UNIQUE (symbol, bar_ts)`. The v4 rebuild adds:

| v4 column | Type | Why |
|---|---|---|
| `decision_id` | TEXT NOT NULL UNIQUE | End-to-end chain key |
| `status` | TEXT NOT NULL | `PENDING`, `APPROVED`, `REJECTED`, `EXPIRED`, `SUPERSEDED`, `EXECUTED`, `FAILED`, or legacy-only `LEGACY` |
| `strategy` | TEXT NOT NULL | Source label, initially `confluence` |
| `timeframe` | TEXT NOT NULL | Bar interval, initially `M15` |
| `entry_price` | REAL | Executable-side quote at signal time |
| `stop_loss` | REAL | Mandatory stop, computed before approval |
| `take_profit` | REAL | Target from the configured trade-management rule |
| `volume` | REAL | Risk-sized volume snapped to broker step |
| `expires_at` | TEXT NOT NULL | Absolute expiry, as defined in §2 |
| `decided_by` | TEXT | `ui`, `telegram`, `expiry`, or `supersede` |
| `decided_at` | TEXT | Decision timestamp |
| `blocks_json` | TEXT NOT NULL | Five strategy-block verdicts |
| `reject_reason` | TEXT | Stable reason code and display text for rejected/failed candidates |
| `superseded_by_decision_id` | TEXT | Newer pending decision that superseded this one |
| `is_test` | INTEGER NOT NULL DEFAULT 0 | Excludes TEST SIGNAL rows from performance statistics |
| `test_tag` | TEXT | `TEST SIGNAL` on synthetic test rows; NULL otherwise |

**v4 migration and old UNIQUE constraint:** SQLite cannot drop
`UNIQUE(symbol, bar_ts)` in place. Before applying v4 to any journal, make a
consistent SQLite backup through one reusable journal-backup helper: request a
WAL checkpoint, then use SQLite's online backup API to create a transactionally
consistent copy (do not copy a live `.db` file without its WAL). The helper
must never overwrite an existing backup. Test it with a temporary WAL journal
containing committed data and verify the backup reopens with matching rows and
integrity. Prove the v4 migration on a separate copy. In the v4 transaction,
create a replacement `signals_v4` table
with the columns above, copy every v3 row and its original ID/symbol/direction/
score/bar timestamp/time, then drop the old table and rename the replacement.
Give each copied row a stable legacy `decision_id` derived from its old row ID,
set its status to `LEGACY` (never accidentally pending), and preserve its
original values. Enforce the constraints with unique `decision_id`, a partial
unique index on `(symbol, strategy, bar_ts) WHERE is_test=0 AND bar_ts IS NOT
NULL`, and a partial unique index on `symbol WHERE status='PENDING'`. The
replacement table permits NULL `bar_ts` only for synthetic test signals
(enforce with a CHECK constraint); non-test strategy rows require a bar
timestamp. Verify row counts, preserved IDs/data, index definitions,
`PRAGMA integrity_check`, and `schema_version=4` on the copy before any
real-journal migration is approved. On any failure, roll back the transaction
and retain the backup; do not silently discard rows or fall back to v3.

For a new actionable candidate, first check the idempotency key. Then, in one
transaction, mark any older PENDING signal for that symbol `SUPERSEDED`,
record its `superseded_by_decision_id`, and insert the new pending candidate.
Do not supersede a pending signal for a candidate that fails history, strategy,
price or risk checks. Existing v3 signals remain LEGACY and do not participate
in the one-pending constraint.

**Failure modes and tests:**

- Same closed bar retried after crash: unique
  `(symbol, strategy, bar_ts)` / `decision_id` prevents duplicates; the
  existing row is returned as an idempotent no-op. Test same bar twice, one row.
- Migration interruption or bad copy: transaction rolls back; test against a
  copied v3 database, including row counts, legacy IDs/status, indexes and
  integrity check; prove the backup remains recoverable.
- Short history after restart: fewer than 206 closed rows yields
  `INSUFFICIENT_HISTORY`, no call to `evaluate`, no signal, and other symbols
  continue. Test at 205 and 206 rows.
- Malformed data / indicator exception: journal a visible per-symbol error;
  no signal row is hidden as "no signal". Test malformed OHLCV and exception.
- No stop or invalid stop distance: no pending signal; preserve a visible
  rejected reason. Test missing tick specification and zero/NaN distance.
- Clock skew: use broker bar close time and UTC expiry arithmetic; test a
  machine clock offset does not lengthen a signal's validity.

## 2. Idempotency and expiry

- **Key:** `(symbol, strategy, bar_close_ts)` maps to the deterministic
  `decision_id` in §1. The database uniqueness constraints are authoritative;
  an in-memory lookup is only an optimization, never the safety guarantee.
- **One pending signal per symbol:** at insertion of a valid replacement,
  atomically mark the old PENDING row SUPERSEDED and link it to the new ID.
  APPROVED signals are no longer pending and cannot be silently superseded;
  they still undergo expiry and approval-time checks before execution.
- **Exact expiry arithmetic:** strategy timeframe is M15, so one bar is
  15 minutes = 900 seconds. Store
  `expires_at = min(bar_close_ts + 900 seconds, signal_created_at + 1800 seconds)`.
  The 30-minute cap is calculated as `30 * 60 = 1800` seconds from creation;
  it prevents delayed processing from granting more than 30 minutes, while
  `bar_close_ts + 900` normally makes the effective lifetime no longer than
  one M15 bar. If the computed expiry is already reached when inserted, store
  the candidate as EXPIRED, not PENDING.
- **Price drift check:** on every engine loop iteration, inspect every PENDING
  row using that iteration's single fresh gateway quote snapshot. Compare the
  signal's entry to the executable side of the quote: ASK for BUY, BID for
  SELL. Expire immediately when
  `abs(current_executable_quote - entry_price) >
  0.5 * abs(entry_price - stop_loss)`. Equality does not exceed the threshold.
  Missing, closed-market, or stale quotes are not treated as zero or as a
  passing check: do not approve/execute without a fresh quote, and retain the
  scheduled expiry as the final bound. The approval handler repeats both the
  time-expiry and drift checks against its current fresh quote immediately
  before risk validation/execution.
- **Supersede and expiry are serialized** with command handling and signal
  insertion by the engine's single writer. Expiry sets status EXPIRED,
  `decided_by='expiry'`, `decided_at`, and a visible reason.

Tests: boundary at exactly half-stop distance and just beyond it for BUY/SELL;
fresh versus stale/missing quote; expiry at bar-close+900 and creation+1800;
already-expired insertion; repeated loop expiry is idempotent; and an approval
at expiry+1 second produces no executor call.

## 3. Risk pre-check and visible rejects

Run the existing risk gate at signal time to size the candidate and again
immediately before execution at approval time. Reuse
`RiskEngine.build_order_intent(...)` / `RiskEngine.evaluate(intent, state)`;
do not create a parallel risk policy.

- **Sequential account state:** refresh account/position state after each
  candidate. Include existing open risk and risk reserved by earlier PENDING
  or APPROVED signals in the same cycle, so two candidates cannot each assume
  the full available budget. Release a reservation on REJECTED, EXPIRED or
  SUPERSEDED; replace it with actual open-position risk after execution.
- **Mandatory stop:** reject a missing stop (`SL_REQUIRED`) or a non-finite,
  zero, or wrong-side stop before approval.
- **Broker sizing:** use each symbol's broker `tick_size`, `trade_tick_value`,
  `trade_contract_size`, `volume_min`, `volume_max` and `volume_step`; do not
  infer specs from display names. Preserve the approved special cases:
  XAUUSD contract size 100, XAGUSD 5000, and JPY pairs tick size 0.001.
  Calculate loss at stop, derive volume under the risk budget, then round down
  to `volume_step`. Never round a too-small size up to `volume_min`.
- **Too-small minimum:** if even `volume_min` exceeds the risk budget, keep the
  candidate visible as REJECTED with the exact card text
  **“Too small to size within the risk limit”** and a stable
  `MIN_VOLUME_EXCEEDS_RISK_BUDGET` reason. It must not disappear or become a
  pending approval.
- **UI rejection reasons:** show the reason and detail for every rejected
  candidate, including `MIN_VOLUME_EXCEEDS_RISK_BUDGET`, `SL_REQUIRED`,
  `INVALID_STOP`, `SPREAD_TOO_WIDE`, `MARKET_CLOSED`, `STALE_QUOTE`,
  `INSUFFICIENT_MARGIN`, `ACCOUNT_GUARD_BLOCKED`, `ENGINE_PAUSED`,
  `ENGINE_HALTED`, `RISK_LIMIT_EXCEEDED`, and `INSUFFICIENT_HISTORY`.
  Preserve the gate's actual reason code; do not relabel an unknown failure as
  success or hide rejected rows.
- At approval, refresh equity/positions and quote, recalculate price, spread,
  volume and risk. Equity/account changes re-run the full gate; widened spread
  rejects with `SPREAD_TOO_WIDE`; closed market, stale quote, paused/halted
  state, or a failed broker precheck blocks execution and is visible on the
  decision chain. No failure sends an order.
- Use risk-sized volume as the default signal volume; it must never exceed a
  configured per-symbol cap. **No per-symbol volume cap currently exists** in
  `config.yaml` or the typed config model, so no value can accurately be
  listed or read today. Do not invent a default, silently use broker
  `volume_max` as a config cap, or edit `config.yaml` without approval. Before
  2b can create/approve signals, add explicit per-symbol caps only after the
  operator supplies values; validate them in the config model and read them
  through the validated engine config alongside `risk` in the sizing path.
  Missing cap configuration must fail closed. Final values and the exact
  config key/path require operator approval.

Tests: formula and broker-step assertions for forex, JPY, XAUUSD and XAGUSD;
minimum volume above budget yields the exact visible text; risk reservation
prevents two sequential signals from overspending; approval-time equity,
spread, market, account-guard and broker-precheck failures all make zero
executor calls.

## 4. Approval flow

1. A valid, risk-sized signal is stored as `PENDING` and rendered as
   **AWAITING_APPROVAL**. A risk-rejected candidate is stored visibly as
   REJECTED and cannot be approved.
2. Dashboard and authorized Telegram actions only enqueue `APPROVE_SIGNAL`
   or `REJECT_SIGNAL` with the `decision_id`; neither surface calls the
   executor. Commands are idempotent and audit-linked to the signal.
3. The single engine writer claims one queued command between scan cycles.
   Approval checks status is still PENDING, checks expiry/drift using a fresh
   quote, verifies mode is PAPER and state/account guards allow a new entry,
   refreshes account state, reruns risk/sizing, then invokes the PAPER
   executor. The executor performs the `order_check`-style broker precheck
   (margin, stops level, volume bounds) against a dry-run request before its
   internal PAPER fill.
4. A successful decision progresses PENDING → APPROVED → EXECUTED. A failed
   re-check is REJECTED or FAILED with the actual reason; an explicit operator
   rejection sets REJECTED; time/drift sets EXPIRED. Every terminal outcome
   records `decided_by`, `decided_at`, reason and the same decision ID.
5. Telegram uses the configured `TELEGRAM_CHAT_ID` as an exact numeric
   allowlist. Reject/ignore any other chat ID before parsing or enqueuing;
   do not authorize by username or message text. If the configured ID is
   absent/invalid, Telegram approvals are disabled. An authorized Telegram
   message names the action and exact decision ID, then queues the same
   command as the dashboard. On receipt and again when the engine claims it,
   re-read signal status and expiry. A message for an expired, superseded,
   already-decided or unknown ID is stale: it never executes or changes the
   signal, is recorded as a failed/stale command, and the authorized sender
   receives a concise stale/already-decided response. Unauthorized senders
   receive no control response.
6. Position monitoring runs on every engine loop iteration, including while
   PAUSED or EMERGENCY_STOP. Existing open-position management, reconciliation,
   equity and heartbeat telemetry remain active; only new entries are gated.
7. The approval handler imports and calls only the PAPER executor
   implementation. It must not import or access a gateway order-submission
   function, the LIVE executor, or any broker `order_send` path. Test with an
   import/call spy that approval cannot reach those symbols, including through
   helper imports; PAPER precheck/fill remains the only execution path.

**Failure modes and tests:**

- Duplicate approvals or UI/Telegram race: conditional command claim plus
  atomic PENDING-to-APPROVED update permits at most one execution; later
  commands fail as already decided. Test both arrival orders.
- Crash after approval is claimed but before execution is durably recorded:
  on restart reconcile the decision ID against order/trade records. If a
  settled order exists, complete the chain idempotently; if execution cannot
  be proven either way, set `NEEDS_REVIEW` and never blindly resubmit. Test
  crash points before precheck, after precheck, and after PAPER fill.
- Approval while paused or halted: command is recorded as rejected; no entry
  or order call. Position monitoring continues.
- Unapproved or unauthorized Telegram chat: no command and no executor call;
  authorized chat ID can enqueue only the exact action/decision ID.
- Stale Telegram message after expiry/supersede/rejection: visible stale result,
  unchanged signal state and no executor call.
- Restart with PENDING rows: each is checked for expiry/drift before any
  approval is accepted; expired rows become EXPIRED. Test stale and fresh rows.
- PAPER `order_check` precheck failure: FAILED with the broker reason and no
  internal fill.

## 5. State machine: no approval transition

**Recommendation: command-handler execution between scan cycles under the
existing single-writer rule. Do not add an approval state or transition.**
Approval does not change scan state: when the engine is IDLE before an
approval, it remains IDLE after the approval handler finishes. In particular,
do not add `IDLE -> VALIDATING` on `SIGNAL_APPROVED`.

The main loop serializes work: monitor existing positions; claim/process
commands while between cycles; if an approval command was processed, finish
that iteration without starting a scan; then scan on the next eligible
iteration. A command arriving during a scan only enters the command queue; the
single writer handles it after the scan has returned to IDLE. Thus a scan and
approval ready in the same wall-clock interval cannot overlap or mutate state
concurrently, and command timing cannot collide with a cycle.

Stage 2 leaves the declared state transitions in `engine/state.py::ALLOWED`
unchanged. For completeness, the current transition table is:

| From | Trigger → To |
|---|---|
| OFF | `ENABLE_AI` → IDLE |
| IDLE | `CYCLE_TICK` → SCANNING; `DISABLE_AI` → OFF; `PAUSE` → PAUSED; `EMERGENCY_STOP` → EMERGENCY_STOP |
| SCANNING | `SCAN_COMPLETE` → ANALYZING; `SCAN_EMPTY` → IDLE; `PAUSE` → PAUSED; `EMERGENCY_STOP` → EMERGENCY_STOP |
| ANALYZING | `SIGNAL_PRODUCED` → VALIDATING; `NO_SIGNAL` → IDLE; `PAUSE` → PAUSED; `EMERGENCY_STOP` → EMERGENCY_STOP |
| VALIDATING | `RISK_APPROVED` → EXECUTING; `RISK_REJECTED` → IDLE; `PAUSE` → PAUSED; `EMERGENCY_STOP` → EMERGENCY_STOP |
| EXECUTING | `ORDER_SETTLED` → MONITORING; `ORDER_FAILED` → IDLE; `PAUSE` → PAUSED; `EMERGENCY_STOP` → EMERGENCY_STOP |
| MONITORING | `CYCLE_COMPLETE` → IDLE; `PAUSE` → PAUSED; `EMERGENCY_STOP` → EMERGENCY_STOP |
| PAUSED | `RESUME` → IDLE; `EMERGENCY_STOP` → EMERGENCY_STOP |
| EMERGENCY_STOP | `RESET_EMERGENCY` → OFF |

These are existing lifecycle transitions, not transitions introduced for
approval-time execution. Tests assert that `SIGNAL_APPROVED` is not added and
that processing either approval outcome leaves IDLE unchanged; a queued
approval during a scan is not handled until the scan completes.

## 6. UI

Signals page (extends the existing Journal table family, read-only reads plus
queued commands):

- Tabs: Pending / Awaiting Approval, Approved, Rejected (including sizing and
  risk rejection reasons), Expired, Superseded, Executed, Failed.
- Each pending row: symbol, direction, entry, SL, TP, volume, risk amount,
  blocks detail, age vs expiry countdown, reject reason where applicable, and
  **Approve / Reject** buttons that only enqueue `APPROVE_SIGNAL` /
  `REJECT_SIGNAL`.
- Decision chain view: one decision ID → signal row, command rows, order row,
  position row, close row.
- Every surface repeats the PAPER disclaimer and shows
  `execution_mode=approve_first` (the status card already does).
- TEST rows carry a prominent `TEST SIGNAL` badge on every surface and are
  visibly distinct from strategy-generated signals.
- Empty states are sentences, never blank space.

Vendor-name guard applies: no provider or model names anywhere; the same AST
test (`tests/test_ui/test_no_vendor_names.py`) covers new strings.

## 7. Brain plug-in (parked; no code now)

The brain becomes **one more signal source in the same pipeline**: it writes a
PENDING row with `strategy='brain'` and the same `decision_id` chain, and
nothing else. It never calls the gateway, never executes, never bypasses
approval, and there is no parallel path.

Prerequisites before it may be enabled (from the parked-branch review):

1. Guard: skip when the account guard is blocked, when the engine is PAUSED or
   EMERGENCY_STOP, and when the gateway is stalled (`last_success_age` over
   the watchdog threshold). Today it checks only `gateway.connected`.
2. A real cost cap (per-call and cumulative). `max_tokens=400` is a length
   cap, not a cost cap.
3. Timeout read from config (`timeout_seconds=25` is a constant today) and a
   declared no-retry / no-fallback policy.
4. Its reasoning text must pass through the same vendor-name filter before any
   UI surface shows it, because LLM text can name a provider.

## 8. TEST SIGNAL fallback (Monday only)

The real deterministic strategy may produce no actionable signal for days.
For a Monday end-to-end verification, if no real strategy signal is available,
an operator may invoke the deterministic TEST SIGNAL injector as a fallback.
It must refuse before writing anything unless mode is exactly PAPER, must
never run in LIVE, and must create a synthetic signal through the same
PENDING → explicit approval → risk re-check → PAPER executor pipeline. It may
not bypass approval or call a separate order path. Use a whitelisted symbol
with a valid broker spec; the test remains contingent on risk approval and the
broker precheck.

Tag the signal row, decision chain, UI card, approval command, journal
lifecycle events and PAPER order/position/close comment with `TEST SIGNAL` and
`is_test=1`. Propagate the same test flag and decision ID to the PAPER
position/trade and its realized and unrealized P&L attribution. Exclude test
trade P&L from every production P&L, win-rate, return and drawdown view,
including account-equity attribution: report broker total equity separately,
and calculate production-attributable equity as broker equity less cumulative
realized and current unrealized TEST SIGNAL P&L. Test trades still count
against current margin and risk limits while open. Keep separate test-only
P&L/equity counters so the exclusion is auditable; never erase or rewrite raw
broker equity snapshots. Test that a synthetic TEST SIGNAL gain/loss changes
raw broker equity but not production-attributable P&L, win rate or drawdown,
and that separate test totals reconcile to the excluded P&L.

The tag is not itself a safety gate: mode and the shared approval/risk checks
remain mandatory. If no real signal exists, record that fact and use only this
labelled test fallback; do not fabricate a strategy-generated signal.

Tests: injector refuses LIVE before any journal/executor call; PAPER injection
creates one tagged pending signal; approval is still required; all chain rows
share its ID/tag; test rows are excluded from all normal P&L, win-rate and
drawdown views, reconcile against raw equity, and appear in test-only counts.

## 9. Crash recovery: needs review

If a crash occurs after an approval is claimed but before execution can be
proved, mark the decision `NEEDS_REVIEW`; do not blindly retry it. The
dashboard must show a persistent warning banner and a status-card line listing
the affected decision IDs, with a link to each decision chain. Clear the
warning only after reconciliation proves the outcome and an operator records
the resolution. Test that an unresolved decision is visible in both locations
after restart, the IDs are present, and a resolved decision clears from the
active warning without deleting its audit history.

## 10. Startup warning for another dashboard

At engine startup, inspect local process command lines and listening endpoints
for another Streamlit process serving this same journal. Match the dashboard
entry point and normalized journal path (and confirm its listener); exclude
the current engine PID. If another matching process is found, write a journal
and log `WARNING` with its PID, endpoint and journal path. Do not stop it,
signal it, or change its database. Do not warn merely because an unrelated
Streamlit app exists. If process/port inspection is unavailable, report that
check as unavailable rather than claiming no duplicate exists.

Tests use mocked process/port records: matching second dashboard warns;
unrelated Streamlit app does not; current process is excluded; inspection
failure is visible and does not stop engine startup.

## 11. Test plan

**Unit:** decision-ID determinism; v4 migration/rebuild on a copied v3 database;
legacy-row preservation; partial unique indexes; supersede and expiry edges;
205/206/300-bar history behavior; sizing and risk reservations by asset class;
exact minimum-volume reject text; state table unchanged; Telegram chat
authorization/stale messages; command idempotency; TEST tagging/statistics
exclusion from all P&L/win-rate/drawdown views; per-symbol cap enforcement;
approval imports restricted to PAPER executor path; Signals-page statuses and
reason strings; needs-review banner/status-card decision IDs;
duplicate-dashboard startup warning.

**Mocked end-to-end (single decision ID):** fake gateway returns one valid
closed M15 history and quote snapshot → signal row written → queued dashboard
approval → expiry/price/risk re-check passes → PAPER precheck and fill →
position monitored → close recorded. Assert one ID across every row, one
pending signal per symbol, no duplicate for `(symbol, strategy, bar_ts)`, and
no approval-driven state transition. Also cover rejection, expiry, halted
approval and TEST SIGNAL exclusion.

**Live TEST SIGNAL chain (Monday, market open):** PAPER only, brain parked,
auto-execute off, approve-first, one whitelisted symbol, no `--live`, no
Stage 2 bypasses, and no change to magic-234000. Prefer a genuine strategy
signal; if none appears, use the tagged TEST SIGNAL fallback above. Manually
approve once through the dashboard or authorized Telegram, verify one PAPER
position and its normal monitoring/close lifecycle, then verify a deliberate
rejection and expiry in separate test signals. No real orders are permitted.

## 12. Weekend expiry guard

A Friday signal does not receive a weekend extension. Its absolute deadline
remains `min(bar_close_ts + 900 seconds, created_at + 1800 seconds)` in UTC.
For example, a last M15 bar closed at Friday 20:45 UTC has a bar-bound expiry
at Friday 21:00 UTC (the creation cap can make it earlier). Sunday/Monday
approval attempts must see EXPIRED and must not execute, even if quotes resume
or the message was delayed. Test with a fixed clock: create the Friday signal,
advance beyond its computed expiry, and submit approval on Sunday and Monday;
both attempts are stale and result in zero executor calls.
