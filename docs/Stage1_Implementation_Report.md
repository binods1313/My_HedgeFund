# Stage 1 implementation report

Written 2026-10-16. Stage 1 code exists in commit `457d3f9`. It was not
loaded into running PID 44436. Stage 2 was not started.

```
457d3f944cfbdce995bd6beaa224e94b8305a49f
feat: add the stage 1 scan cycle on temporary databases
8 files changed, 661 insertions(+), 5 deletions(-)
```

Files in that commit:

```
engine/__main__.py
engine/config.py
engine/gateway.py
engine/journal.py
engine/scan_cycle.py
tests/test_engine/test_engine_loop_throttle.py
tests/test_engine/test_scan_cycle_stage1.py
tests/test_ui/test_data_access.py
```

The commit message states the live journal was not migrated. The
2026-10-15 read-only extract agrees: `schema_version` rows are version 1
and version 2, both `2026-10-13T19:51:15+00:00`. Live `scan_results`
columns were `id`, `symbols_json`, `ranked_json`, `ts`. Count was 0.
`scan_symbols` was not on the live file.

PID 44436 started 15-10-2026 18:53:46. `engine/scan_cycle.py` was last
written 15-10-2026 20:16:11. The process is still that 18:53:46 process.
Restarting it to load Stage 1 was refused.

## Implemented

In the source tree, not in PID 44436.

### Schema v3

`engine/journal.py` sets `SCHEMA_VERSION = 3`. `MIGRATION_3` is
forward-only and runs when a `Journal` is constructed on a database
older than 3.

```sql
ALTER TABLE scan_results ADD COLUMN cycle_id TEXT
ALTER TABLE scan_results ADD COLUMN status TEXT
ALTER TABLE scan_results ADD COLUMN reason TEXT
ALTER TABLE scan_results ADD COLUMN symbol_count INTEGER

CREATE TABLE IF NOT EXISTS scan_symbols (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id  TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    status    TEXT NOT NULL,
    reason    TEXT NOT NULL DEFAULT '',
    bar_ts    TEXT,
    ts        TEXT NOT NULL
)
CREATE INDEX IF NOT EXISTS idx_scan_symbols_cycle ON scan_symbols (cycle_id, symbol)
CREATE INDEX IF NOT EXISTS idx_scan_results_cycle ON scan_results (cycle_id)
```

Old `scan_results` columns stay. A design note that put per-symbol rows
inside `scan_results` was not followed. The header is `scan_results`.
The per-symbol rows are `scan_symbols`.

Constructing `Journal` on `data/engine_5057151780.db` would apply this
migration under the live writer. That constructor was not used on the
live file.

### scan_results

One header per attempt that is journaled. Insert columns:

```
symbols_json, ranked_json, ts, cycle_id, status, reason, symbol_count
```

`symbols_json` and `ranked_json` are written as `"[]"`. Status values
used by the cycle include `skipped`, `empty`, and `recorded`.

### scan_symbols

One row per symbol once a cycle has started and has a `cycle_id`.
Status is `skipped`, `deferred`, `error`, or `scanned`. A precheck skip
does not write symbol rows. It writes one header only, through
`_note_skip`.

### Cycle tick

`Engine.run_once` order in this tree:

1. `commands.poll_once()`
2. account transition check and `_verify_account("session_refresh")`
3. heartbeat
4. `_write_telemetry()`
5. `_scan_cycle()`

`_scan_cycle` no-ops when `scanner is None`. `build_engine` now sets

```python
scanner = ScanCycle(
    symbols=list(config.scan_symbols),
    max_scan_seconds=float(config.max_scan_seconds),
)
```

When the precheck passes, the cycle transitions `IDLE` to `SCANNING`
with trigger `CYCLE_TICK`. It always returns to `IDLE` with trigger
`SCAN_EMPTY` if it entered `SCANNING`. It does not emit a completion
trigger that would enter analyzing. `engine/state.py` still lists a
future rule for that later trigger. `scan_cycle.py` does not emit it.
A throttle test asserts the literal completion token is absent from
`engine/scan_cycle.py`. The module docstring was reworded so the token
is not in that file.

Default `ScanCycle.interval_seconds` is 60. `cycle_interval_seconds` is
not a key in `config.yaml`. Production construction passes symbols and
`max_scan_seconds` only, so the 60-second default applies.

### Gateway snapshot

`Gateway.snapshot(symbols)` performs one owner-thread read.
`market_open` is `trade_mode == SYMBOL_TRADE_MODE_FULL` (4). The snapshot
also carries `stale`, `bar_ts` (the tick `time` as a string, or null),
`bid`, and `ask`. This MetaTrader5 build has no `symbol_info_session_*`
functions. The snapshot does not invent session hours.

The cycle calls `read_market` once per started cycle. It does not follow
that with more terminal reads.

### Market closed ordering

`_one_symbol` checks `market_open` before staleness. The default for a
missing `market_open` is false, so a closed (or unknown) symbol is
recorded as `MARKET_CLOSED` and is not recorded as `STALE_QUOTE`.

### Per symbol isolation

An exception in one symbol becomes that symbol's `error` row
(`"{Type}: {message}"`). The loop continues. If every symbol errors, the
cycle still finishes and, if it had entered `SCANNING`, returns with
`SCAN_EMPTY`.

If the terminal drops mid-cycle, the current symbol and the rest are
`deferred` with `TERMINAL_OFFLINE`, and a guard event is written. If the
account check fails mid-cycle, the loop breaks with `ACCOUNT_MISMATCH`
and does not write further symbol rows.

### Time budgeting

`max_scan_seconds` defaults to 10 on the dataclass. When the monotonic
clock shows the budget is exhausted, the rest of the symbols are
`deferred` with `CYCLE_BUDGET_EXHAUSTED`. One scan event,
`cycle budget exhausted`, is written for that overrun. The heartbeat for
the iteration has already been written, because the scan is after the
heartbeat.

### Skip rows

`_precheck` order:

1. mode is not PAPER → `NOT_PAPER`
2. state is not IDLE → `STATE_{current}`
3. inside `interval_seconds` of the last attempt → `CYCLE_INTERVAL`
4. empty symbol list → `NO_SYMBOLS`
5. terminal not up → `TERMINAL_OFFLINE`
6. writes not allowed → `ACCOUNT_MISMATCH`
7. otherwise one `read_market`

A skip journals one cycle-level header when the reason changes, or when
`remind_every` seconds have passed for that same reason. Default
`remind_every` is 600. The reminder test uses `last is None or (now_m -
last >= remind_every)`. An earlier sentinel of `not 0.0` was true for a
zero timestamp and was corrected to the `None` check.

### Reminder rows

Those skip headers are the Stage 1 reminder rows. They are not the
account-guard reminders on events 23631–23637. The guard uses its own
`_REMIND_EVERY = 600` in the code PID 44436 is running. Stage 1 reminders
exist only in the new module and were exercised on temporary databases.

Production `compute` is `None`. A symbol that passes the filters and has
no compute function is counted as scanned and does not call a strategy.

The cycle does not call `send_order` or `order_send`.

## Tested

Temporary databases only. Label: **TESTED-ONLY**.

Recorded focused run after the docstring correction: `26 passed` in
1.67s. The stage-1 module contains these tests:

```
test_f14_one_market_read_for_five_symbols
test_f3_one_symbol_error_does_not_stop_the_cycle
test_f4_all_symbols_error_returns_scan_empty
test_f1_terminal_drop_defers_the_rest_and_returns_idle
test_f2_account_switch_blocks_further_writes_and_submits_nothing
test_f5_budget_defers_and_still_leaves_the_heartbeat
test_f11_same_closed_bar_skips_without_recompute
test_market_closed_is_recorded_before_stale_quote
test_state_change_writes_a_skip_and_a_repeat_waits_ten_minutes
```

Placement of the call inside `run_once`, after the heartbeat:

```
tests/test_engine/test_engine_loop_throttle.py
test_scan_step_stays_inside_run_once_after_the_heartbeat
```

`tests/test_ui/test_data_access.py` asserts `schema_version == 3` on the
database that test opens. That is a test database.

Broader recorded run, `tests/test_engine` and `tests/test_ui`:
`1012 passed, 6 skipped` in 24.82s.

Covered by those tests, on temporary databases:

- one market read for five symbols (F14)
- one symbol error does not stop the cycle (F3)
- all symbols error still returns `SCAN_EMPTY` (F4)
- terminal drop defers the rest and returns to IDLE (F1)
- account switch blocks further writes and submits nothing (F2)
- budget deferral still leaves the heartbeat already written (F5)
- same closed bar skips without recompute (F11)
- market-closed is recorded before stale-quote
- a state-change skip is written, and a repeat waits ten minutes

## Excluded

Not in Stage 1. Not implemented in `engine/scan_cycle.py`.

- spread filter
- corrupt-quote filter beyond the snapshot `stale` flag (missing tick or
  missing bid)
- duplicate exposure
- max open trades
- news blackout
- a strategy, a signal, or a PAPER stop-loss / take-profit
- F6, F8, F9, F10, F12, F13
- emitting the later completion trigger that would leave IDLE toward
  analyzing
- `cycle_interval_seconds` in `config.yaml`
- wiring `ui/data_access.py` `JOURNAL_TABLES` to the new scan tables
- changing `system_status()["scan_loop"]`, which still returns the
  literal `"Not built yet"` (`ui/data_access.py` around the status
  payload, shown by the header as `Scan loop: Not built yet`)

`engine/research/agent_04_news.py` (commit `720f616`) counts impact
strings already on `context["events"]`. It does not fetch a calendar.
Empty events return `ok=False`, label `NONE`, error `no event data`.
That is a docstring correction of existing research code. It is not a
Stage 1 scan filter.

## Not yet deployed

| Item | Evidence |
|---|---|
| Stage 1 code is on the branch | Commit `457d3f9` |
| Stage 1 code is in PID 44436 | No. Process start 18:53:46 is before the file. Process was not restarted. |
| Live schema | Versions 1 and 2 only. Not version 3. |
| Live `scan_symbols` | Not present in the read-only extract. |
| Live `scan_results` rows | Count 0, old columns. |
| UI scan-loop badge | Still the string `Not built yet`. |
| Stage 2 | Not started. |

`tests/test_dependencies.py::TestAppRunsEndToEnd` was deselected
(`93 passed, 2 deselected`). Running it constructs `Journal` on the
configured live path from the header and would migrate the live file.

## Blocked

- Loading Stage 1 into the running process. That requires a restart.
  Restart was not approved. PID 44436 stays up.
- Migrating the live database. The writer is on schema 2. Opening
  `Journal` would apply migration 3.
- Treating the temporary-database tests as a live scan. They are
  TESTED-ONLY.
- Starting Stage 2. Explicitly out of scope.

The whitelist names that end in `#` are not offered on this
MetaQuotes-Demo terminal. That is a symbol-survey fact
(`docs/Live_Symbol_Survey_5057151780.md`). It is not a Stage 1 code
defect, and this report does not recommend a configuration change.
