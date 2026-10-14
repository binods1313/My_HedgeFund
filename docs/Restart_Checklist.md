# Restart Checklist

Use this checklist when restarting the system after changes or after a controlled stop.

## Pre-Restart
- [ ] Verify git status is clean (or expected)
- [ ] Run tests: py -m pytest -q (expect 1550 passed, 6 skipped, 0 failed)
- [ ] Confirm PAPER mode is enabled (auto-execute OFF, approve-first ON)
- [ ] Confirm magic-234000 positions remain untouched (never delete/modify)
- [ ] Check engine is not running (Get-Process -Name "python" -ErrorAction SilentlyContinue | Where-Object { .CommandLine -match "engine" })
- [ ] Review recent journal entries for anomalies

## Start
- [ ] Start engine: py -m engine (or via start script)
- [ ] Start UI: streamlit run app.py (separate terminal)
- [ ] Verify engine single instance (check logs)
- [ ] Confirm state transitions: OFF -> IDLE after ENABLE_AI (only via button)
- [ ] Check heartbeat online
- [ ] Confirm account guard working (no false positives)

## Post-Start Validation
- [ ] Status card shows correct state (engine IDLE, PAPER, heartbeat online)
- [ ] No new account_mismatch rows (check journal/reconcile)
- [ ] trades_today shows 0 or None appropriately (None if DB issue, 0 if empty)
- [ ] Research Desk shows honest reasons (disabled/not built yet as applicable)
- [ ] Emergency stop available and functional
- [ ] Journal page accessible and rendering correctly

## Shutdown
- [ ] Stop UI (Ctrl+C in Streamlit terminal)
- [ ] Stop engine (Ctrl+C in engine terminal)
- [ ] Verify no orphaned Python processes
- [ ] Archive logs if needed
