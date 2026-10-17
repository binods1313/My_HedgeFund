# Stage 2 Design — Signal to Paper Execution

**Status: DESIGN ONLY. No code until approved.** PAPER only, no `--live`,
brain parked, `execution_mode: approve_first`, magic-234000 untouched.

Scope: a deterministic signal fires on a closed bar, waits for a human
decision, and only then reaches the PAPER executor. Every step is journaled
under one decision ID so a signal can be traced from bar to closed position.

---

## 1. Signal generation (deterministic, closed bars)

**Where:** `engine/scan_cycle.py` already calls a `compute` hook per symbol
after the skip filters (`SYMBOL_NOT_FOUND`, `TRADE_DISABLED`, `MARKET_CLOSED`,
`STALE_QUOTE`) and the new-closed-bar check. Stage 2 fills that hook.

**What:** for each of the 16 whitelisted symbols with a new closed bar, build
the OHLCV frame (`gateway` owner-thread read, never a second MT5 session) and
call `engine/strategy.py::evaluate(symbol, df)`. `Signal.is_actionable` gates
the write; conflicts reject rather than down-weight.

**Decision ID:** `decision_id = uuid5(NAMESPACE, f"{symbol}|strategy:confluence|{bar_close_ts}")`.
Deterministic: re-running the same bar yields the same ID, so a retry is a
no-op, not a duplicate. The ID is stamped on the signal row, the approval
command, the order, and the position close.

**Journal write — schema v4 (add columns only):**

Existing `signals` table: `id, symbol, direction, score, bar_ts, ts` with
`UNIQUE (symbol, bar_ts)`.

| v4 column | Type | Why |
|---|---|---|
| `decision_id` | TEXT UNIQUE | the chain key |
| `status` | TEXT NOT NULL DEFAULT 'PENDING' | PENDING → APPROVED / REJECTED / EXPIRED / EXECUTED / FAILED |
| `strategy` | TEXT NOT NULL DEFAULT 'confluence' | source label for later sources |
| `entry_price` | REAL | price at signal time |
| `stop_loss` | REAL | mandatory, computed before approval |
| `take_profit` | REAL | from trade_manager multiplier |
| `volume` | REAL | from the risk sizing below |
| `expires_at` | TEXT | computed at insert, see §2 |
| `decided_by` | TEXT | `ui` / `telegram` / `expiry` |
| `decided_at` | TEXT | |
| `blocks_json` | TEXT | the five block verdicts, for the UI detail view |

**Failure modes and tests:**

- *Duplicate bar reprocessed after a crash*: v4 has
  `UNIQUE (decision_id)`; insert is `INSERT OR IGNORE`, and the handler treats
  "row already exists" as a no-op. Test: run the same bar twice, one row.
- *Indicator exception on a thin frame*: `evaluate` raises on `min_rows`;
  caught per symbol, the scan row is `skipped` with reason
  `SIGNAL_ERROR:<name>` and the cycle continues. Test: 5-row frame → skip row,
  no signal row, other symbols still processed.
- *Signal with no stop distance*: `to_intent` already refuses a missing tick
  spec; a zero/NaN stop distance is rejected before any row is written. Test:
  flat frame → no row.
- *Clock skew*: `bar_ts` is the broker bar time, not local time; expiry maths
  uses broker `tick.time`. Test: local clock 2h ahead does not expire a fresh
  signal.

## 2. Idempotency and expiry

- **Key:** `(symbol, strategy, bar_close_ts)` → deterministic `decision_id`
  (§1). The v4 UNIQUE constraint is the hard backstop; the in-memory check
  (`SELECT 1 FROM signals WHERE decision_id = ?`) avoids the write entirely.
- **One pending signal per symbol:** before inserting, mark any existing
  PENDING row for that symbol EXPIRED (`decided_by='superseded'`). Test: two
  consecutive actionable bars for EURUSD leave exactly one PENDING row.
- **Expiry:** a PENDING signal expires at `min(bar_close_ts + one bar,
  insert_time + 30 min)`, or immediately if `|current_price - entry_price|
  > 0.5 * |entry_price - stop_loss|` on any loop read. Expiry is evaluated by
  the engine loop every cycle and by the approval command before acting, so a
  stale approval can never execute. Test: approval at expiry+1s → command
  FAILED with `signal expired`, no order call.

## 3. Risk pre-check

Run at **signal time** (to size and price the card) and again at **approval
time** (to reject what moved).

- Existing gate: `RiskEngine.build_order_intent(...)` → `RiskEngine.evaluate(intent, state)`.
  Reuse both; no parallel sizing code.
- Account state is rebuilt between sequential signals:
  `account_state_from_positions(...)` is re-read from the journal after every
  signal write, so two same-cycle signals cannot both assume the full budget.
- **Mandatory stop-loss:** an intent without `stop_loss` is refused
  (`SL_REQUIRED` exists today).
- **Sizing from broker specs** (per symbol, from `symbol_info`): XAUUSD
  contract 100, XAGUSD contract 5000, JPY pairs tick 0.001. Volumes are snapped
  to `volume_step`; a size below `volume_min` after risk maths is a reject, not
  a silent round-up. Test table: one row per asset class
  (forex 100000/1e-5, JPY 0.001, XAUUSD 100, XAGUSD 5000) asserting computed
  volume equals the formula within one step.
- Approval-time re-check failure modes: equity moved → re-evaluate against the
  current account; spread widened → `SPREAD_TOO_WIDE`; market closed →
  command FAILED with `market closed`. Each gets a test.

## 4. Approval flow

1. Signal stored as `status='PENDING'` (shown in UI as AWAITING_APPROVAL).
2. New queued commands (dashboard writes only; engine polls as today):
   - `APPROVE_SIGNAL {decision_id}` — queued from the Signals page or Telegram
   - `REJECT_SIGNAL {decision_id}` — same sources
3. On `APPROVE_SIGNAL`, the engine, in order: loads the row → checks
   `status='PENDING'` → re-checks expiry → re-runs the risk gate and the live
   price check → calls the PAPER executor. The PAPER executor performs the
   `order_check`-style broker precheck described earlier (margin, stops level,
   volume bounds) against a dry-run request before filling internally.
4. Outcomes journal as command rows plus signal status: EXECUTED /
   FAILED(reason). `REJECT_SIGNAL` sets REJECTED.
5. **Position monitoring runs on every loop iteration regardless of state**,
   including PAUSED and EMERGENCY_STOP: trailing stop, breakeven, partial
   close, and equity/heartbeat telemetry are read-and-journal paths that must
   not depend on the trading state machine. Only *new entries* are gated.

**Failure modes and tests:**

- *Approval arrives twice*: second command finds status != PENDING → FAILED
  `already decided`. Test: approve twice → one order, one failure.
- *Approval while halted*: command polls still run in EMERGENCY_STOP (today's
  behavior), but approval of an entry in a halted engine is refused; close-only
  actions remain allowed. Test: halt, approve → FAILED `engine halted`.
- *Telegram and UI race*: both enqueue; dedupe by `dedupe_key=decision_id`
  (column exists in `commands`). Test: two rows, one claimed.
- *Engine restart with PENDING signals*: on startup, PENDING rows older than
  expiry are marked EXPIRED before the loop starts. Test: seed stale row,
  start, assert EXPIRED.

## 5. State machine change (minimal, explicit)

Current table (`engine/state.py::ALLOWED`) has no approval step. Stage 2 adds
exactly one transition, and it is declared in the table, not hard-coded:

```
STATE_IDLE:    + Rule(STATE_VALIDATING, "SIGNAL_APPROVED")
STATE_VALIDATING: (unchanged — RISK_APPROVED → EXECUTING is the existing exit)
```

Rationale: approval re-validation *is* the VALIDATING state; no new state is
needed. Signals do not change state on their own — a PENDING signal with the
engine IDLE leaves the state machine at IDLE until a human approves.

Test: `allowed_from("IDLE")` includes VALIDATING only via `SIGNAL_APPROVED`;
`SIGNAL_APPROVED` from any other state raises IllegalTransition.

## 6. UI

Signals page (extends the existing Journal table family, read-only reads plus
queued commands):

- Tabs: Pending, Approved, Rejected, Expired, Executed.
- Each pending row: symbol, direction, entry, SL, TP, volume, risk amount,
  blocks detail, age vs expiry countdown, **Approve / Reject** buttons that
  write `APPROVE_SIGNAL` / `REJECT_SIGNAL` rows — nothing else touches the
  order path.
- Decision chain view: one decision ID → signal row, command rows, order row,
  position row, close row.
- Every surface repeats the PAPER disclaimer and shows
  `execution_mode=approve_first` (the status card already does).
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

## 8. Test plan

**Unit:** decision-ID determinism; supersede/expiry edges; sizing table per
asset class; state table entries; command dedupe; UI strings.

**Mocked end-to-end (single decision ID):** fake gateway feeds one closed bar
with a valid setup → signal row written → mocked UI approval command → risk
re-check passes → PAPER fill → position monitored → external close → one
decision ID visible on every row. Assert no second row exists for the same
`(symbol, bar_ts)`, and that the halted-engine approval was refused.

**Live TEST SIGNAL chain (Monday, market open):** one whitelisted symbol,
smallest volume step, PAPER mode, observed end to end in the journal; then a
deliberate rejection and a deliberate expiry on the next two signals. Success
criteria: exactly one PAPER position, correct sizing for the symbol's tick
spec, no orders after halt, no duplicate rows.
