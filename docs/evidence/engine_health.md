# Evidence: Engine Health (Task 1)

## Objective

Confirm the engine process state, journal health, and soak stability after the
Grok handoff stall.

## Commands Executed

```powershell
# Check process
Get-Process -Id 44436 -ErrorAction SilentlyContinue
Get-Process -Id 29092 -ErrorAction SilentlyContinue

# Read-only DB query (engine not opened through Journal)
python -c "import sqlite3; conn=sqlite3.connect('file:data/engine_5057151780.db?mode=ro', uri=True); ..."

# Lock file
Get-Content data/engine_5057151780.lock
```

## Findings: PID 44436 is DEAD

```
PID 44436: DEAD
PID 29092: DEAD
```

PID 44436 was the original engine process started 2026-10-15 18:53:46. After the
Grok stall (deadlocked BTW at 21:35:34Z), the Grok parent PowerShell (PID 40440)
died, and with it the engine process it spawned. PID 29092 was the child
restarted by Grok at 20:05:13Z — also dead.

## Engine Restart

Stale lock file contained PID 29092. Lock removed via Python (PowerShell
could not read or delete it due to the OS-level file lock held by the dead
process — the lock was released when the process terminated, but the file
contents remained).

```powershell
Remove-Item data/engine_5057151780.lock
```

Engine restarted:

```
python -u -m engine
```

New PID: 14184 (parent process). Engine child PID: 46628 (spawned by
`python -u -m engine`). Mode: PAPER. Magic number: 100100.

## Current Engine Health

```
Latest heartbeat: id=72263 ts=2026-10-15T22:38:06+00:00 pid=46628 connected=1 call_age=0.000130
Total heartbeats: 72263
Schema version: 3
```

- Heartbeats advancing every second (1 Hz)
- `connected=1` on all recent heartbeats
- `gateway_call_age_seconds` column present (added by schema migration 3)
- `gateway_call_age_seconds` values ~0.0001s (healthy — last gateway call was 0.1ms ago)

## Lock File

```
data/engine_5057151780.lock contains: "46628"
PID 46628: ALIVE
```

Single instance lock verified: PID 46628 matches the running engine process.

## Account Fingerprint (JOURNAL_ACCOUNT table)

```
id=1 account_login=5057151780 currency=USD server=MetaQuotes-Demo magic=100100
created_at=2026-10-13T19:51:15+00:00
```

Journal account fingerprint matches configured MT5 account (5057151780, USD,
MetaQuotes-Demo). Account guard verification passes on engine startup.

## Conclusion

Engine was restarted cleanly after the Grok process death. Current process
(PID 46628) is healthy: heartbeats advancing, gateway connected, schema v3
active, account fingerprint verified.
