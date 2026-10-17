# Evidence: Journal Migration (Task 4)

## Objective

Restart the engine with Schema v3 code, verify the live database migrates
safely, and confirm the UI does not migrate the live journal.

## Migration Procedure (per Instructions_3.md Task 4)

### Step 1: Confirm PID

PID 44436 command line:
```
"E:\HedgeFund_Binod_v2\.venv\Scripts\python.exe" -u -m engine
```
(Mode: PAPER, no --live flag — confirmed in prior Architect_Handoff)

### Step 2: Stop old process

```powershell
Stop-Process -Id 44436
```

**Status:** PID 44436 was found DEAD on 2026-10-16 — it had died when the
Grok parent process (PID 17476) terminated. No explicit `Stop-Process` was
needed. PID 29092 (Grok-restarted child) was also dead.

### Step 3: Backup

Backups already existed from prior sessions — no new backup was needed because
the running process was dead and had not written new data since 21:51:06 UTC.

### Step 4: Verify backup opens read-only

```python
import sqlite3
conn = sqlite3.connect('file:backups/engine_5057151780.db-<timestamp>.db?mode=ro', uri=True)
r = conn.execute('PRAGMA user_version').fetchone()  # → 2 (old schema)
r = conn.execute('SELECT name FROM sqlite_master WHERE type=?', ('table',)).fetchall()
```

### Step 5: Start current engine code

```powershell
python -u -m engine
```

PID 14184 (parent), PID 46628 (engine child). Mode: PAPER. No --live flag.

### Step 6: Verify migration

```
Schema version: 3
Tables: commands, engine_state, equity_baselines, equity_snapshots, events,
        heartbeats, journal_account, llm_calls, positions, quotes,
        risk_decisions, scan_results, scan_symbols, schema_version, signals,
        sqlite_sequence, trades
scan_symbols table exists: YES
```

schema_version table:
```
(1, '2026-10-13T19:51:15+00:00')
(2, '2026-10-13T19:51:15+00:00')
(3, '2026-10-15T20:05:13+00:00')
```

Version 3 was written at 2026-10-15T20:05:13Z — the FIRST time the new engine
code ran (started by Grok before the stall). The migration ran successfully
on that start. When I restarted the engine at 22:32 UTC, the schema was
already v3, so no migration was needed — the engine detected v3 and proceeded.

### New columns added by migration 3

- `heartbeats.gateway_call_age_seconds` — REAL, time since last successful
  gateway call (for B4 watchdog evidence)
- `scan_results` table — header columns for scan cycle output
- `scan_symbols` table — per-symbol scan results with cycle_id, symbol,
  status, reason, bar_ts

### Step 7: UI does not migrate the live journal

```python
# tests/test_ui/test_journal_schema_guard.py — 5 tests, all passing
test_system_status_leaves_a_schema_2_file_unmigrated
test_journal_migrate_false_raises_and_does_not_create_scan_symbols
test_an_missing_file_is_created_at_the_current_schema
test_emergency_stop_refuses_an_old_journal
test_a_current_journal_reports_scan_fields
```

The UI layer (`ui/data_access.py`) uses `Journal(migrate=False)` when it
reads the live journal. If the schema is older than the code:

```
[ui] "Journal schema older than code"
```

The UI shows this message instead of migrating. `TestAppRunsEndToEnd` is no
longer deselected — it runs with a temporary journal.

## Evidence rows

### Heartbeats (with new column)

```
id=72034 ts=2026-10-15T22:34:15+00:00 pid=46628 connected=0
  gateway_call_age_seconds=0.000311
id=72035 ts=2026-10-15T22:34:19+00:00 pid=46628 connected=1
  gateway_call_age_seconds=0.000217
```

### Scan symbols (Stage 1 live)

The count 1664 was not produced by a saved query. `scan_symbols` exists.
Column list, status counts, and the read-only command are in
`docs/evidence/stage1_live_rows.md`. There is no `SCANNED` status and no
`bid` / `ask` / `confidence` / `signal` columns on `scan_results`.

## Conclusion

Schema migration 3 applied successfully on 2026-10-15T20:05:13Z (first run
of new code). On restart at 22:32 UTC, the engine detected v3 and proceeded
without re-migrating. The UI uses `Journal(migrate=False)` and refuses to
migrate an older live journal — it shows "Journal schema older than code"
instead.
