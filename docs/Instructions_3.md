2a accepted. Copy migration, row counts, integrity check, indexes, backup helper and 1616 tests are good. I APPROVE the live migration and sub-stage 2b. PAPER only, no --live, approve-first, auto-execute off, brain parked, do not touch magic-234000. Mocked tests first.

1. COMMIT FIRST
Commit the v4 migration, backup helper and their tests in logical commits (migration, backup helper, tests). Paste git log --oneline -6 and git status. Nothing uncommitted may run in the live engine.

2. LIVE MIGRATION PROCEDURE (report each step with raw output)
a. Verify the engine is healthy now: newest heartbeat id and ts (read-only), PID and command line, MT5 connected.
b. Backup with backup_journal() into backups/ named engine_5057151780_pre_v4_<UTC timestamp>.db, and paste read-only counts of the backup. Also copy the existing pre-v4 backup from the session folder into backups/.
c. Stop the engine: confirm the command line, Stop-Process -Id for the parent, confirm the child is gone, and confirm the lock is released.
d. Start the engine on current code from the project venv (PAPER, no --live). It migrates the live journal to v4 as the single writer. Paste: schema_version rows, signals table SQL, both partial index definitions, PRAGMA integrity_check, row counts of every table versus the backup, startup events, and new heartbeats advancing with pid.
e. Restart the dashboard from run.bat on 8502 only if it shows errors after the migration. Check the Journal page shows v4 and the Signals table renders empty with a sentence.
f. If anything fails: stop the engine, do not retry silently, report, and restore only with my approval.

3. CONFIG (separate commit, tell me before you apply): add a per-symbol volume cap setting. Default cap per symbol = volume_min x 10. Show the exact key name and where it is read, the validation (must be >= volume_min, loud failure otherwise) and a test. No other config changes.

4. SUB-STAGE 2b (after the migration is verified)
- Gateway history read: one batched owner-thread read per cycle, only for symbols with a newly closed M15 bar, 300 closed bars, never the forming bar. Test read count equals symbols with a new bar.
- Signal writing: strategy evaluate() only when at least 206 closed rows; INSUFFICIENT_HISTORY visible per symbol at 205; PENDING rows only; deterministic decision_id; supersede one pending per symbol; idempotent on the same bar; expiry min(bar_close+900s, created+1800s); drift check each loop with ASK for BUY and BID for SELL; rejected candidates stored visibly with reasons including the exact text "Too small to size within the risk limit".
- Banner and status-card line for any decision that needs review.
- Signals page, read-only for now: tabs and a decision chain view. NO approval buttons, NO executor changes, NO approval commands yet (that is 2c).
- Weekend test, and test that the signal path imports no gateway order function.
- Vendor-name guard passes for all new strings.

REPORT: one-page table (PROVEN / PROBABLE / TESTED-ONLY / UNPROVEN) then raw output with exact commands, full suite numbers. Stop after 2b. Do not start 2c.