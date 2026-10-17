# Round 2 Code Change Log

For every changed file in commit `cfc08b0` (the B1/B4/purge/execution-mode/journal-guard package).

Commit: `cfc08b0` — "fix: recover a dropped terminal and watch a stalled gateway"

---

## File: engine/gateway.py

**Reason:** Fix B1 (terminal recovery) and B4 (gateway stall watchdog).

**Change summary:**
- Added `max_account_reinitializes` parameter (default 3) — bound on None
  account_info() retries before the gateway waits on the longest backoff only
- Added `watchdog_timeout` parameter (default 30.0) — B4 stall detection threshold
- Added `_empty_account_reads` counter, `_inflight_name`, `_inflight_started`,
  `_stall_logged`, `_reconnect_requested` instance fields
- `account_info()`: if gateway claims connected but account_info() returns None,
  calls `_on_empty_account()` instead of trusting the stale connected flag
- `_on_empty_account()`: sets connected=False, account_readable=False,
  increments counter, queues session drop if within retry cap, schedules backoff
- `_queue_drop_session()`: submits a "reinitialize" request to the owner thread
  to call `_drop_session_inline()` (mt5.shutdown) — never runs on the caller thread
- `_drop_session_inline()`: shuts down MT5 session on the owner thread
- `poll_watchdog()`: detects when the inflight call exceeds watchdog_timeout,
  sets connected=False, logs WARNING with kind=gateway_stalled, sets
  _reconnect_requested=True (one-shot via _stall_logged)
- `last_success_age()`: returns seconds since last_success_at (for heartbeat column)
- `send_order()`: rejects order_send when _reconnect_requested is True (NO retry
  after stall) — prevents ghost orders
- `_handle()`: checks _reconnect_requested before order_send, clears it for
  non-order calls, calls _drop_session_inline to reset connection
- `connected` property now reads from `status.connected`

**Tests:**
Tests: `tests/test_engine/test_b4_watchdog.py` (3 tests) +
`tests/test_engine/test_b1_recovery.py` (3 tests, partial coverage of gateway)

---

## File: engine/account_guard.py

**Reason:** Extend the Step A guard. This module is not new in `cfc08b0`.

`git log -- engine/account_guard.py` shows the file was added in `b773324`
(`feat(guard): account fingerprint guard with offline/mismatch split and write gates`).
`cfc08b0` changes 35 lines on top of that file. The fingerprint type, the
four `may_*` methods, and the 600-second reminder window belong to Step A
(`b773324`), not to this commit. The "3,161 rows / 53 min" description
belongs to Step A as well.

**Change in `cfc08b0` (`git show cfc08b0 -- engine/account_guard.py`):**
- Adds `_active_kind` and `_outage_started` on `AccountGuard`.
- On a return to a matching fingerprint after `terminal_offline` or
  `account_mismatch`, writes one INFO event `Terminal recovered`
  (`kind=terminal_recovered`) with `downtime_seconds`, then clears those fields.
- `_reject()` records `_outage_started` once and sets `_active_kind` to
  `terminal_offline` or `account_mismatch`.
- Does not add `may_modify_orders()` or `may_write_telemetry()`. Those
  methods already return `self.verified` and are not called from
  `emergency_stop.py` or `enable_ai.py` in this commit.

A later change on this branch, not in `cfc08b0`, queues emergency stop and
reset with a direct `INSERT` into `commands` when the journal schema is
older than the code. `cfc08b0` itself still opens `Journal(migrate=False)`
and re-raises `SchemaTooOld`.

**Tests:** `tests/test_engine/test_b1_recovery.py` covers the recovery event
added here. `tests/test_engine/test_account_guard.py` predates this commit.

---

## File: engine/journal.py

**Reason:** Schema v3 migration with gateway_call_age column + migration guards.

**Change summary:**
- `SCHEMA_VERSION = 3`
- Migration 3: adds `gateway_call_age_seconds REAL DEFAULT 0` to `heartbeats`,
  creates `scan_results` table, creates `scan_symbols` table
- `SchemaTooOld` / `SchemaTooNew` exceptions
- `Journal(migrate=False)`: read-only mode for UI — opens existing journal
  without writing migration rows or creating new tables
- `write_heartbeat()`: accepts `gateway_call_age_seconds` parameter
- `get_account_fingerprint()` / `set_account_fingerprint()`: typed accessors
  for the journal_account table
- `log_event(category, message, severity, source, context)`: structured event
  logging with JSON context

**Tests:** `tests/test_ui/test_journal_schema_guard.py` (5 tests)

---

## File: engine/config.py

**Reason:** Add gateway watchdog/reinit settings + execution_mode validation.

**Change summary:**
- `GatewaySettings`: added `account_reinitialize_attempts` (default 3, range 1–30)
  and `watchdog_timeout_seconds` (default 30.0, range 0–600)
- `_parse_execution_mode()`: accepts only `approve_first` (default); any other
  value raises ValueError — `auto_execute` is a config error at load time
- `EXECUTION_MODE_APPROVE_FIRST = "approve_first"` constant
- `EngineConfig.execution_mode` field

**Tests:** `tests/test_engine/test_execution_mode.py` (3 tests)

---

## File: engine/__main__.py

**Reason:** Wire account guard into the engine loop.

**Change summary:**
- `run_once()`: calls `self.guard.check(account, phase)` every cycle
  — if `TerminalOffline` or `AccountMismatch`, the heartbeat is still
  written but with connected=False and no telemetry
- Heartbeat writer passes `gateway_call_age_seconds` from
  `Gateway.last_success_age()`
- Account verification report in main() startup (`_report_account_check()`)
- `build_engine()` constructs `AccountGuard` with the configured fingerprint
  and magic number

**Tests:** Integration coverage via test_b1_recovery, test_b4_watchdog

---

## File: engine/state.py

**Reason:** Pass gateway call age into the heartbeat row.

**Change in `cfc08b0` (`git show cfc08b0 -- engine/state.py`, 6 lines, +4/-2):**
- `heartbeat(self, connected: bool = False)` becomes
  `heartbeat(self, connected: bool = False, gateway_call_age: float | None = None)`.
- The method passes `gateway_call_age` through to `self.journal.heartbeat`.
- The diff does not edit the transition table and does not mention
  `STATE_EMERGENCY_STOP`.

---

## File: engine/config.py (existing, updated)

**Reason:** See above (gateway settings + execution_mode).

---

## File: scripts/purge_engine_db.py

**Reason:** B1-adjacent safety — prevent purging the live journal.

**Change summary (187 lines, rewritten):**
- Requires account number confirmation (must match Journal journal_account)
- Requires database filename confirmation
- Checks lock file — refuses if PID in lock is alive
- Checks backup age — refuses if latest backup is older than 1 hour
- Uses `--yes` flag to skip confirmations (for automation, but still requires
  both checks to pass programmatically)
- All file I/O wrapped in try/except with clear error messages
- Uses `Journal.migrate=False` for read-only account lookup

**Tests:** `tests/test_purge_engine_db.py` (5 tests)

Commit `d361bc7` — "fix: point the purge script at the configured journal"
pre-dated cfc08b0 and corrected the db_path resolution.

---

## File: ui/data_access.py

**Reason:** UI safety — never migrate the live journal.

**Change summary:**
- `system_status()`: reads journal with `Journal(migrate=False)`
- If schema version < code's SCHEMA_VERSION: returns
  `"Journal schema older than code"` — does NOT migrate
- `execution_mode` read from config and returned to dashboard
- `scan_running`, `last_cycle_ts`, `symbols_scanned`, `symbols_skipped`
  added to system_status output (replacing "Scan Loop: Not built yet")

**Tests:** `tests/test_ui/test_journal_schema_guard.py` (5 tests)

---

## File: ui/components/header.py

**Reason:** Display execution_mode status.

**Change summary:**
- 4 lines added: reads `config.execution_mode` and displays "APPROVE-FIRST"
  in the header status bar
- No behavioral change to trading logic

---

## File: ui/components/emergency_stop.py

**Reason:** Leave an older journal unmigrated.

**Change in `cfc08b0` (`git show cfc08b0 -- ui/components/emergency_stop.py`, 14 lines, +10/-4):**
- `queue_emergency_stop` and `queue_reset_emergency` change
  `Journal(target)` to `Journal(target, migrate=False)`.
- `SchemaTooOld` is imported and re-raised as `SchemaTooOld(SchemaTooOld.TEXT)`.
- The diff does not call `guard.may_modify_orders()`. That method lives on
  `AccountGuard` and is not referenced by this file in `cfc08b0`.

`cfc08b0` still refuses a schema-2 file: `Journal(migrate=False)` raises
before `enqueue_command`. A later change on this branch replaces that
`Journal()` construction with a direct `INSERT` into `commands` (the table
created in migration 1) and does not migrate.

---

## File: ui/components/enable_ai.py

**Reason:** Leave an older journal unmigrated.

**Change in `cfc08b0` (`git show cfc08b0 -- ui/components/enable_ai.py`, 7 lines, +5/-2):**
- `queue_enable_ai` changes `Journal(target)` to `Journal(target, migrate=False)`.
- `SchemaTooOld` is imported and re-raised as `SchemaTooOld(SchemaTooOld.TEXT)`.
- The diff does not call `guard.may_write_telemetry()`. That method lives on
  `AccountGuard` and is not referenced by this file.

---

## File: tests/test_engine/test_b1_recovery.py (NEW)

**Reason:** Tests for B1 recovery.

3 tests covering outage→recovery, heartbeat connected=0 during outage,
mismatch does not auto-recover, telemetry resumes, exactly one event each.

---

## File: tests/test_engine/test_b4_watchdog.py (NEW)

**Reason:** Tests for B4 watchdog.

3 tests covering default timeout (30s, 3 attempts), blocked owner call
triggers stall without order retry, next read reconnects without order.

---

## File: tests/test_engine/test_execution_mode.py (NEW)

**Reason:** Tests for execution_mode validation.

3 tests covering default=approve_first, auto_execute rejected, mode change
logged as one INFO row.

---

## File: tests/test_purge_engine_db.py (NEW)

**Reason:** Tests for purge script safety.

5 tests covering both confirmations required, stale backup refusals, held
lock refusals, unlocked file detection, --yes behavior.

---

## File: tests/test_ui/test_journal_schema_guard.py (NEW)

**Reason:** Tests for UI journal migration guard.

5 tests in `cfc08b0` covering a schema 2 file left unmigrated,
`migrate=False` raises, a missing file created at the current schema,
emergency stop refusing an old journal, and the current journal reporting
scan fields. A later change on this branch replaces the refusal test:
halt and reset insert a `commands` row on a schema-2 file and do not migrate.

---

## Commit hashes

| File | Commit |
|---|---|
| engine/gateway.py, account_guard.py, journal.py, config.py, __main__.py, state.py | cfc08b0 |
| tests/test_engine/test_b1_recovery.py, test_b4_watchdog.py, test_execution_mode.py | cfc08b0 |
| tests/test_ui/test_journal_schema_guard.py | cfc08b0 |
| scripts/purge_engine_db.py (rewrite) | cfc08b0 (preceded by d361bc7) |
| tests/test_purge_engine_db.py | cfc08b0 |
| ui/data_access.py, components/header.py, emergency_stop.py, enable_ai.py | cfc08b0 |
| tests/test_engine/test_account_guard.py | cfc08b0 (updated) |
| tests/test_engine/test_gateway.py | cfc08b0 (updated) |
| tests/test_engine/test_telemetry.py | cfc08b0 (updated) |
| tests/test_ui/test_status_card_honesty.py | cfc08b0 (updated) |
| tests/test_dependencies.py | cfc08b0 (updated) |
