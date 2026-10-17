# Open issues and blockers

Living list. Written 2026-10-16 from the 2026-10-15 evidence package.
Documentation only. Nothing in this file was fixed by writing it.

Owner is "Not assigned" until an architect names one. Status dates use
the evidence day, not a new measurement on 2026-10-16. The journal was
not re-queried on 2026-10-16. PID 44436 was still the process created
15-10-2026 18:53:46 when this list was written.

## B1 — No recovery event generated

| Field | Value |
|---|---|
| ID | B1 |
| Severity | High |
| Description | After the intentional terminal close, the engine wrote `Terminal offline` and reminders. When an independent probe could read account 5057151780 again, the engine still had no recovery row. The newest engine event stayed unresolved. |
| Evidence | Event 23630 at `2026-10-15T14:16:55+00:00`, kind `terminal_offline`. Reminders 23631–23637. Equity frozen at id 31346. Paired read: heartbeat 57799 at `2026-10-15T17:15:01+00:00`, `connected=1`, state IDLE, event 23637 still `Terminal offline (still unresolved)`, probe login 5057151780 on MetaQuotes-Demo. `check()` success clears `_active_reason` and does not insert a row. `_ensure_connected` returns immediately when `status.connected` is already true. |
| Risk | The journal can say the terminal is offline after the terminal is logged in. Telemetry stays blocked. A later reader can treat `connected=1` heartbeats as proof the outage ended. |
| Owner | Not assigned |
| Status | Open. Automatic recovery of PID 44436 is BLOCKED. A reconnect change was not applied. |

## B2 — PID 44436 is not running Stage 1

| Field | Value |
|---|---|
| ID | B2 |
| Severity | High |
| Description | Commit `457d3f9` adds the Stage 1 scan cycle. The running process started before that file existed and was not restarted. |
| Evidence | CreationDate 15-10-2026 18:53:46. `engine/scan_cycle.py` last write 15-10-2026 20:16:11. Command line `python -u -m engine` with no `--live`. Same PID confirmed still present on 2026-10-16. |
| Risk | Reviewing the source tree and concluding the live loop scans, journals `scan_symbols`, or enforces the Stage 1 filters. It does not. |
| Owner | Not assigned |
| Status | Open. Restart was not approved. Stage 2 was not started. |

## B3 — Live schema is still version 2

| Field | Value |
|---|---|
| ID | B3 |
| Severity | High |
| Description | The module constant is `SCHEMA_VERSION = 3`. The live file still has schema versions 1 and 2. Opening it with `engine.journal.Journal` would apply `MIGRATION_3` while PID 44436 is writing the old shape. |
| Evidence | Read-only extract: `schema_version` rows `(1, 2026-10-13T19:51:15+00:00)` and `(2, 2026-10-13T19:51:15+00:00)`. No `scan_symbols` on the live file. `scan_results` columns `id`, `symbols_json`, `ranked_json`, `ts`. `COUNT(*)` was 0. `tests/test_ui/test_data_access.py` expects version 3 on the database that test opens. `TestAppRunsEndToEnd` was deselected because the header constructs `Journal` on the configured path. |
| Risk | A test, a UI reload, or a manual `Journal()` call migrates the live file under the old writer. |
| Owner | Not assigned |
| Status | Open. Live file not migrated. |

## B4 — Heartbeat gap investigation

| Field | Value |
|---|---|
| ID | B4 |
| Severity | Medium |
| Description | The heartbeat gap for PID 44436 spans the Windows Modern Standby interval. |
| Evidence | Heartbeats `57478` at `2026-10-15T15:35:46+00:00` and `57479` at `2026-10-15T17:08:53+00:00`; Windows records Modern Standby from 21:05:41 to 22:38:53 IST, matching the MT5 disconnect/re-authorization. See the raw event output in `docs/Architect_Handoff_2026-10-15.md`. |
| Risk | Engine heartbeats pause while the host is asleep; they resume after wake. A blocked owner-thread call is not evidenced and is not attributed to this gap. |
| Owner | Resolved by evidence review |
| Status | Closed. Sleep/wake explanation PROVEN. |

## B5 — Offline reminder timing investigation

| Field | Value |
|---|---|
| ID | B5 |
| Severity | Medium |
| Description | Account-guard reminders are supposed to repeat every 600 seconds while the offline reason stays unresolved. The first reminders did. Later ones did not. |
| Evidence | 23630 `2026-10-15T14:16:55+00:00`. Then 23631 14:26:55Z, 23632 14:36:56Z, 23633 14:46:56Z, 23634 14:56:57Z. Next is 23635 `2026-10-15T15:20:52+00:00`, then 23636 15:30:53Z, then 23637 `2026-10-15T17:08:52+00:00`. The 17:08:52Z reminder sits next to the heartbeat resume at 17:08:53Z. `_REMIND_EVERY = 600` is the guard constant in the running code, not the Stage 1 skip reminder. |
| Risk | Silence alone is not the same thing as recovery. The heartbeat gap itself is explained by sleep/wake; the separate reminder-spacing anomaly remains unresolved. |
| Owner | Not assigned |
| Status | Open. The spacing change is PROVEN. The cause is UNPROVEN. |

## B6 — Approve-first configuration uncertainty

| Field | Value |
|---|---|
| ID | B6 |
| Severity | Medium |
| Description | Design notes talk about approve-first and auto-execute. The running process does not read an `approve_first`, `auto_execute`, or `execution_mode` key. |
| Evidence | Search of `*.py`, `*.yaml`, and `*.yml` on 2026-10-16: no matches for those three names. The phrases are in `docs/Instructions_2.md` and `docs/Phase2A_Scan_Loop_Design.md`. Startup events 23625 and 23628 say `telegram approval layer enabled`. That is the optional telegram layer, recorded at process start. It is not a config key with that name. Commands since 13:23:46Z: count 0. No order was sent this run. |
| Risk | An architect can believe a YAML switch already chooses approve-first versus auto-execute for PID 44436. No such switch was found in the code this process loads. |
| Owner | Not assigned |
| Status | Open. UNPROVEN as an executed policy. The absence of those keys in code and YAML is PROVEN for the 2026-10-16 search. |

## Also open, not in the required six

These are recorded so the list matches the handoff. They are not extra
work started by this document.

| ID | Severity | Description | Evidence | Risk | Owner | Status |
|---|---|---|---|---|---|---|
| B7 | Low | Exact locked byte of `data\engine_5057151780.lock` was not printed. | Size 5. `read(64)` raised `PermissionError` `[Errno 13]`. A 2026-10-16 search skipped the file with os error 33. A byte-0 probe can print `LOCK_FREE` and still be wrong, because the lock is taken at the then-current offset before the PID is written at offset 0. | A second engine start could be attempted on a false `LOCK_FREE`. | Not assigned | Open. UNPROVEN byte. Lock held is PROVEN. Do not start a second engine. |
| B8 | Low | Live Streamlit badge for stored CRITICAL was not opened. | Unit test `test_archived_mismatch_without_kind_renders_critical_not_info` passed. Commit `7616d0f`. Page not opened. | The HTML test can pass while a long-lived Streamlit process still has the old function. | Not assigned | Open. TESTED-ONLY. |
| B9 | Low | `ui` scan-loop status string is stale relative to committed Stage 1. | `ui/data_access.py` returns `"scan_loop": "Not built yet"`. | A dashboard can say the scan is absent after a future process loads Stage 1. | Not assigned | Open. Not changed. The string is accurate for PID 44436. |
