# Phase 2A — Scan Loop Design

**Status: DESIGN ONLY. No code has been written for this phase. Nothing in
`engine/` emits `CYCLE_TICK` today. This document is here to be approved,
amended or rejected before a line of it is implemented.**

The immediate trigger: the state machine already declares the whole cycle —
`IDLE → SCANNING → ANALYZING → VALIDATING → EXECUTING → MONITORING → IDLE` —
and nothing triggers it. `engine/state.py:72` defines
`Rule(STATE_SCANNING, "CYCLE_TICK")`; a grep for `CYCLE_TICK` across
`engine/__main__.py` returns nothing. The machine is wired for a cycle that
does not run, which is why Phase 2A starts here and not with the proposer:
`STATE_ANALYZING` is reachable only from `STATE_SCANNING`, so a strategy
built first would have no way to be invoked.

---

## 0. Scope boundary, stated first because it is the thing most likely to be misread

| | |
|---|---|
| **In scope** | The loop that reads the market, filters, decides, and manages PAPER positions |
| **Out of scope** | Real order routing. DEMO. Auto-execution. Any change to the magic-234000 positions |
| **PAPER fills are simulated** | A PAPER fill is a journal row this process writes to itself. It proves the *decision*, never the *execution* |
| **Approval is mandatory** | `approve-first`. Auto stays off. No code path in this phase submits without a human |
| **Foreign positions** | Never touched, in any state, by any step below |

**The distinction this phase must not blur:** every test below is green in
PAPER while MT5 is unproven. A green suite is evidence about the loop, not
about the broker. That is already pinned by `tests/test_engine/test_reality_gate.py`,
and the UI states it on every PAPER surface. The DEMO gate in §11 is where
broker execution gets proven, separately and deliberately later.

---

## 1. The cycle, and who emits each transition

Nine states exist in `engine/state.py`. Every arrow below is already declared;
the right-hand column is the gap — nothing emits these yet.

| # | From | Trigger | To | Who emits the trigger |
|---|---|---|---|---|
| 1 | `OFF` | `ENABLE_AI` | `IDLE` | **Operator**, dashboard or Telegram. Already wired. |
| 2 | `IDLE` | `CYCLE_TICK` | `SCANNING` | **The scan loop** (`engine/__main__.py::run_once`), after all gates in §2 pass |
| 3 | `SCANNING` | `SCAN_COMPLETE` | `ANALYZING` | **The scan loop**, when ≥1 symbol survives the filters |
| 4 | `SCANNING` | `SCAN_EMPTY` | `IDLE` | **The scan loop**, when 0 symbols survive |
| 5 | `ANALYZING` | `SIGNAL_PRODUCED` | `VALIDATING` | **The strategy**, on a closed-bar signal |
| 6 | `ANALYZING` | `NO_SIGNAL` | `IDLE` | **The strategy** — the common case |
| 7 | `VALIDATING` | `RISK_APPROVED` | `EXECUTING` | **The risk engine** (`engine/risk.py`) |
| 8 | `VALIDATING` | `RISK_REJECTED` | `IDLE` | **The risk engine**, reason journalled |
| 9 | `EXECUTING` | `ORDER_SETTLED` | `MONITORING` | **The executor**, on a simulated PAPER fill |
| 10 | `EXECUTING` | `ORDER_FAILED` | `IDLE` | **The executor** |
| 11 | `MONITORING` | `CYCLE_COMPLETE` | `IDLE` | **The scan loop**, after position management (§8) |
| — | any | `PAUSE` / `RESUME` | `PAUSED` / `IDLE` | **Operator** |
| — | any | `EMERGENCY_STOP` | `EMERGENCY_STOP` | **Operator**, no confirmation |
| — | `EMERGENCY_STOP` | `RESET_EMERGENCY` | `OFF` | **Operator**, typed `RESET` |

Two rules make this safe to build:

1. **The loop never calls `transition` for a state it does not own.** It emits
   `CYCLE_TICK`, `SCAN_COMPLETE`, `SCAN_EMPTY`, `CYCLE_COMPLETE`. The strategy
   emits `SIGNAL_PRODUCED`/`NO_SIGNAL`. The risk engine emits
   `RISK_APPROVED`/`RISK_REJECTED`. The executor emits `ORDER_SETTLED`/
   `ORDER_FAILED`. Each component owns exactly its own arrows, so an
   undeclared jump fails loudly instead of silently corrupting the state.
2. **`SCANNING → EXECUTING` must fail.** It is not declared, and `is_allowed`
   rejects it. The strategy cannot skip risk validation by being handed a
   scan result directly.

---

## 2. When a scan may run

A cycle is attempted only if **every** condition holds. Any failure is a skip
with the reason journalled, never an exception and never a retry-storm.

| Gate | Condition | If false |
|---|---|---|
| State | `IDLE` | skip — a cycle is in progress, or the engine is not armed |
| Armed | `mode` is `PAPER` | skip — this phase does not scan on a live account |
| Terminal | gateway online, account readable | skip `TERMINAL_OFFLINE` |
| Account guard | `guard.verified`, no `mismatch_reason` | skip `ACCOUNT_MISMATCH` |
| Halted | state is not `EMERGENCY_STOP` | skip `HALTED` |
| Paused | state is not `PAUSED` | skip `PAUSED` |

A skip writes **one** `scan_results` row (`status='skipped'`, `reason=<gate>`)
rather than one row per symbol: the skip is about the engine, not about any
symbol. The guard's 10-minute reminder throttle already prevents a persistent
outage from flooding the journal — 500 identical rejections produce one event.

**Ordering matters.** The state gate is checked first and cheaply; the
terminal gate is last, because touching the gateway is the expensive,
fallible step.

---

## 3. One market reader, in the owner thread

**No component except the gateway owner thread reads the market.** Not the
strategy, not a filter, not a new scanner module. Two MT5 callers in one
process is the failure mode this phase exists partly to prevent: interleaved
`copy_rates` calls against a shared C bridge is where the previous system
corrupted its own quotes.

```
run_once()                      # engine loop, one iteration
 └─ gateway.snapshot()          # ONE market read, owner thread only
     ├─ bars per watched symbol
     ├─ tick/spread per symbol
     └─ account_info
        │
        └─ a plain frozen snapshot object, handed down to:
             ├─ filters   (§6)        - read the snapshot, never the terminal
             ├─ strategy  (§7)        - read the snapshot, never the terminal
             └─ position manager (§8) - read the snapshot, never the terminal
```

The snapshot is built once per cycle and treated as immutable. A second read
inside the same cycle is a bug, and the test in §12 asserts the gateway's
read methods are called exactly once per cycle regardless of how many
symbols exist.

---

## 4. Closed bars only

- A bar is used only after it has **closed**. The forming bar is never a
  signal source; a strategy reading an open bar sees a value that will change
  under it and will look correct in backtest.
- `config.yaml` already carries `scanner.scan_cadence: on_new_bar`. The loop
  computes on a newly closed bar and skips otherwise, journalling
  `reason='no_new_closed_bar'` rather than recomputing identical inputs.
- The bar's broker timestamp is validated against the terminal clock, using
  the existing validated-clock helper. A symbol whose bars are a day stale is
  skipped as `STALE_QUOTE`, not traded on — that was a live defect on the XM
  demo account, where forex and index ticks lagged while crypto was correct.

---

## 5. Per-symbol error isolation

One bad symbol must never stop a cycle.

```python
for symbol in watched:                      # inside the loop body, see §9
    budget_left = cycle_budget.remaining()
    if budget_left <= 0:
        journal_scan(symbol, status="deferred", reason="CYCLE_BUDGET_EXHAUSTED")
        continue                            # defer, do not abort
    try:
        with snapshot.for_symbol(symbol) as view:
            ...
    except Exception as exc:                # one symbol is not the cycle
        journal_scan(symbol, status="error",
                     reason=f"{type(exc).__name__}: {exc}")
        continue                            # the next symbol still runs
```

Properties: the `try` is **inside** the per-symbol iteration; a failure
produces a row and a `continue`; the cycle completes and emits
`SCAN_COMPLETE`/`SCAN_EMPTY` as normal. If *every* symbol errors, the result
is `SCAN_EMPTY` with N error rows — not a crash, and not a state the machine
cannot represent.


---

## 6. Filters, each with a journalled reason

Every filter writes the reason to the symbol's `scan_results` row. A symbol
that produces no signal and a symbol that was filtered are both recorded,
which is what makes a quiet week explainable after the fact.

| Filter | Reject reason | Source |
|---|---|---|
| Spread | `SPREAD_TOO_WIDE` | live ask−bid vs `max_spread_points` (per-symbol override) |
| Quote sanity | `CORRUPT_QUOTE` | spread > `max_spread_pct_of_ask` (5% of ask) |
| Staleness | `STALE_QUOTE` | bar/tick age over threshold, or clock mismatch |
| No closed bar | `NO_NEW_CLOSED_BAR` | nothing new to compute |
| Duplicate exposure | `DUPLICATE_OPEN_POSITION` | this system already holds this symbol |
| Capacity | `MAX_OPEN_TRADES` | `risk.max_open_trades` (currently 5) reached |
| News blackout | `NEWS_BLACKOUT` | high-impact event inside the window |
| Market closed | `MARKET_CLOSED` | weekend / broker session closed for this symbol |
| Symbol unusable | `SYMBOL_NOT_QUOTABLE` | broker lists it but serves no live feed |
| Budget | `CYCLE_BUDGET_EXHAUSTED` | deferred to the next cycle |

Blackout and session windows are read from the journal's news cache and the
broker's session data. **A missing blackout calendar fails open to "no
blackout"** — refusing to scan because a cache is empty would mean the engine
goes silent on a cache bug — **but a missing session read fails closed**,
because "cannot tell if the market is open" must not become "assume it is".

---

## 7. Strategy on closed bars

- Runs per surviving symbol, over the frozen snapshot, on closed bars only.
- Emits `SIGNAL_PRODUCED` → `VALIDATING`, or `NO_SIGNAL` → `IDLE`.
- **The strategy never submits.** It returns an `OrderIntent`. The risk engine
  validates it; a human approves it; the executor fills it. Three separate
  components, in that order, with no shortcut.
- A signal carries a `decision_id` that every later row references (§10).
- No signal is the normal case and is journalled as such, with the strategy's
  stated reason. An engine that logs only trades cannot be audited for the
  trades it declined to make.

---

## 8. PAPER position management

PAPER positions are journal rows. Managing them means comparing their SL/TP
against live quotes from the same single snapshot.

```python
for position in journal.positions():        # managed magic only, see §11
    view = snapshot.for_symbol(position["symbol"])
    hit = which_first(position["sl"], position["tp"], view.bid, view.ask)
    if hit is None:
        continue
    journal.close_managed_position(
        ticket=position["ticket"],
        reason=hit,                           # "SL" or "TP"
        decision_id=position["decision_id"],  # same id as the entry
        price=view.price_for(hit),
    )
```

- **SL/TP both touched in one snapshot is not resolved optimistically.** The
  snapshot has no intra-bar ordering, so the loop cannot know which was hit
  first. It closes at the **adverse** one and journals
  `reason='AMBIGUOUS_SL_TP'`. A fill model that guesses favourably is a fill
  model that lies about drawdown.
- The close reuses the **same `decision_id`** as the entry, so one decision's
  full history — signal, approval, entry, SL/TP hit, exit — is one query.
- Foreign-magic positions are not in `journal.positions()` at all; the guard
  never writes them, and the emergency sweep re-checks magic anyway.


---

## 9. Interval and time budget

Two independent knobs, both in `config.yaml`:

```yaml
scanner:
  scan_cadence: on_new_bar     # existing
  cycle_interval_seconds: 60   # floor between attempts; not a target
  max_scan_seconds: 10         # existing — per-cycle wall-clock budget
```

**The budget protects the loop, not the scan.** A scan that overruns must not
starve heartbeats or command polling — an operator's `EMERGENCY_STOP` must be
seen within one poll, not after a slow cycle finishes. So:

1. The budget is checked **before each symbol**, not after the loop. A symbol
   is either scanned within budget or deferred with
   `CYCLE_BUDGET_EXHAUSTED`.
2. The scan step is **inside the loop body**, indented, so a throttle cannot
   be mistaken for a second pass. This is deliberate: a version with the scan
   dedented past the throttle passed review and silently skipped it, and the
   defect is invisible at runtime. `tests/test_engine/test_engine_loop_throttle.py`
   asserts the indentation on the real source, because a test of behaviour
   alone would have passed.
3. Heartbeats and `poll_once()` run at the **top** of `run_once`, before any
   scan work. A slow cycle delays the next scan, never the next heartbeat.
4. Overrun is journalled once per cycle with the count deferred, not once per
   symbol.

---

## 10. `scan_results` — one row per symbol per cycle

The current table is one row per *cycle* with a JSON blob:

```sql
CREATE TABLE IF NOT EXISTS scan_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbols_json TEXT NOT NULL,      -- cycle-level blob
    ranked_json  TEXT NOT NULL DEFAULT '[]',
    ts           TEXT NOT NULL
)
```

That shape cannot answer "why was EURUSD not traded last Tuesday?" without
parsing JSON. **Phase 2A adds a per-symbol row**, keeping the existing
columns for compatibility:

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | row id |
| `cycle_id` | TEXT | one UUID per cycle; groups the rows |
| `ts` | TEXT | cycle timestamp |
| `symbol` | TEXT | broker symbol name |
| `status` | TEXT | `scanned` \| `no_signal` \| `filtered` \| `error` \| `deferred` \| `skipped` |
| `reason` | TEXT | filter/reject code, or `''` when traded |
| `signal` | TEXT | `BUY` \| `SELL` \| `''` |
| `decision_id` | TEXT | set when a signal survives; links to `signals`/`orders`/`fills` |
| `spread_points` | REAL | observed spread, for the audit trail |
| `bar_close_ts` | TEXT | the closed bar the decision used |
| `duration_ms` | INTEGER | per-symbol cost, for budget tuning |
| `detail_json` | TEXT | indicator values, error text, ambiguity notes |

**"No signal" is a row.** `status='no_signal'`, `reason='STRATEGY_NO_SETUP'`.
A symbol that was filtered has `status='filtered'` and the filter code. This
is the table that turns "the engine did nothing this week" from a shrug into
an answerable question.

---

## 11. Approval — approve-first, always

- Auto-execute is **off** in this phase and stays off. `auto_execute: false`
  in `config.yaml`, and the executor refuses to fill without an approved row.
- A surviving signal lands in `signals` with `status='AWAITING_APPROVAL'`.
- Approval comes from the **dashboard** (an Approve button on the row) or
  **Telegram**. Both write the same approval row; neither bypasses risk.
- Approval is a separate human act from the scan that produced it. A signal
  may go stale — if its bar has closed and the market has moved, the
  executor re-checks price and refuses with `STALE_SIGNAL` rather than
  filling an old decision at a new price.
- Rejection is journalled with the same `decision_id`. Declining a signal is
  a decision and is recorded as one.

**PAPER fills are simulated, and the UI says so** on every PAPER surface: a
PAPER fill is a row this process writes about itself. It does not prove MT5
will accept, fill, or hold an order.

**The later DEMO gate — separate, approve-first, tiny.** Not in this phase.
When it comes: a distinct mode; its own account; a size cap far below any
PAPER-equivalent; an explicit approval on the *gate itself* before any DEMO
scan runs; and proof requirements — a live fill, a live SL/TP modification,
a live close, and a reconnect — each observed once on the real bridge before
DEMO is treated as anything but an experiment.

---

## 12. Failure modes and the test for each

| # | Failure | Required behaviour | Test |
|---|---|---|---|
| F1 | Terminal goes offline mid-cycle | remaining symbols defer; state returns to `IDLE`; one guard event | gateway drops after 2 symbols → `IDLE`, 1 event |
| F2 | Account switches mid-cycle | every write blocked; no order submitted | fingerprint flips → `ACCOUNT_MISMATCH`, no fill |
| F3 | One symbol raises | that symbol gets an `error` row; cycle continues | bad symbol among 4 → 3 scanned, 1 error |
| F4 | All symbols raise | `SCAN_EMPTY`, N error rows, no crash | all raise → `SCAN_EMPTY` |
| F5 | Cycle overruns budget | deferred rows, one overrun event; **heartbeat still written** | slow scan → heartbeat present, budget event once |
| F6 | Emergency stop mid-cycle | `EMERGENCY_STOP` accepted from any state; sweep closes managed only | stop in `SCANNING` → halted, foreign untouched |
| F7 | Throttle indentation regresses | the source-level test fails at collection | scan step dedented → `test_engine_loop_throttle` fails |
| F8 | SL and TP in one snapshot | close at the adverse level, `AMBIGUOUS_SL_TP` journalled | both touched → adverse fill, ambiguity row |
| F9 | Second engine starts | refused by the instance lock, fatal, no heartbeat written | lock held → second process exits |
| F10 | Signal goes stale pre-approval | `STALE_SIGNAL`, no fill | approve after bar closed → refused |
| F11 | No new closed bar | skip with reason; no recompute | same bar twice → second is a skip |
| F12 | Risk rejects | `RISK_REJECTED`, reason journalled, no fill | over max trades → rejected with reason |
| F13 | Foreign position in journal | never closed by any path, skip journalled | foreign row + halt → still open |
| F14 | Gateway read called twice | per-cycle call count asserted | 5 symbols → exactly 1 market read |

F5 and F9 are the two that protect the operator: a slow scan must never delay
a heartbeat, and a second engine must never write to the journal.

---

## 13. What each step writes, and how the UI shows it

| Step | Journal write | UI surface |
|---|---|---|
| Cycle attempted | `scan_results` row per symbol + per-cycle `cycle_id` | Journal → `scan_results`, filter by symbol or `status` |
| Signal produced | `signals` row, `AWAITING_APPROVAL`, `decision_id` | Journal → `signals`; Approve/Reject on the row |
| Approved / rejected | approval row, same `decision_id` | `signals` row shows who and when |
| PAPER fill | `orders` + `fills`, `broker='PAPER'` | Journal → `orders`/`fills`, PAPER labelled |
| Position open | `positions`, `magic=100100` | Journal → `positions` |
| SL/TP hit | close row + event, same `decision_id` | `positions` + `events` |
| State change | lifecycle event via the state machine | System Status + `events` |
| Skip | `scan_results` `status='skipped'` + reason | `scan_results` |
| Emergency stop | lifecycle event + per-position close | System Status shows halted; `events` |

**What the System Status card must gain** to be honest about a running loop:
`scan_running`, `last_cycle_id`, `last_cycle_ts`, `symbols_scanned`,
`signals_awaiting_approval`, `open_positions`, `emergency_stop_state`,
`cycle_budget_exhausted`. Today `system_status()` carries engine, account and
schema fields only — `engine_state`, `mode`, `heartbeat_age_s`,
`schema_version`, `account_login_masked`, `pending_commands` — and a card
that shows `engine_online: true` next to nothing about scans would imply a
loop exists. It does not. Those fields arrive with the loop that makes them
true, not before.

---

## 14. Explicitly not in this document

- `engine/brain.py` — not created, not referenced, not planned here.
- The scan loop implementation. This is a design; it needs approval first.
- Any change to `MODE_LIVE` behaviour, or any auto-execute path.
- Any order, modification or close that could reach a foreign magic number.
- The DEMO gate implementation — only its outline (§11).

---

## 15. Open questions for the architect

1. **`cycle_interval_seconds: 60`** — is a 60s floor right for the watched
   symbols, or should it follow `on_new_bar` strictly with the interval only
   as a safety net?
2. **SL/TP ambiguity (F8).** Closing at the adverse level is conservative but
   will overstate drawdown in PAPER. Accept, or record both and defer the
   close to the next tick?
3. **`scan_results` migration.** Add per-symbol rows alongside the existing
   cycle-level columns, or replace the shape and rewrite the reader?
4. **News blackout source.** The journal cache, or a live fetch? A stale
   blackout calendar is worse than none.
5. **Approval expiry.** How stale may a signal be before it is auto-rejected
   rather than shown to a human?


- Auto-execute is **off** in this phase and stays off. `auto_execute: false`
  in `config.yaml`, and the executor refuses to fill without an approved row.
- A surviving signal lands in `signals` with `status='AWAITING_APPROVAL'`.
- Approval comes from the **dashboard** (an Approve button on the row) or
  **Telegram**. Both write the same approval row; neither bypasses risk.
- Approval is a separate human act from the scan that produced it. A signal
  may go stale — if its bar has closed and the market has moved, the
  executor re-checks price and refuses with `STALE_SIGNAL` rather than
  filling an old decision at a new price.
- Rejection is journalled with the same `decision_id`. Declining a signal is
  a decision and is recorded as one.

**PAPER fills are simulated, and the UI says so** on every PAPER surface: a
PAPER fill is a row this process writes about itself. It does not prove MT5
will accept, fill, or hold an order.

**The later DEMO gate — separate, approve-first, tiny.** Not in this phase.
When it comes: a distinct mode; its own account; a size cap far below any
PAPER-equivalent; an explicit approval on the *gate itself* before any DEMO
scan runs; and proof requirements — a live fill, a live SL/TP modification,
a live close, and a reconnect — each observed once on the real bridge before
DEMO is treated as anything but an experiment.


