Previous task mostly done (scorer fixes, review gate accepted). Remaining items below. PAPER only, no --live, approve-first, auto-execute off, brain parked, do not touch magic-234000, DEMO blocked, no push. Do not edit strategy rules, stop formula or TP. Do not restart the engine without a backup and verified PID. No new documents (docs/Stage2b_Live_Report.md only). Report only what you ran, raw output; "not run" otherwise.

1. HEALTH CHECK, RAW SQL (the last report had no numbers and contradicted earlier evidence):
 a. SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now'); newest heartbeat id and ts; engine_state row.
 b. newest scan_results id, ts, status; COUNT(*) of scan_results with ts in the last 60 minutes; newest 5 scan_results rows.
 c. COUNT(*) of events with message LIKE '%evaluate decision%' in the last 60 minutes and in the last 24 hours; newest such event id and ts; the exact SQL you used. Earlier journals had hundreds of strategy_evaluation events per day, so "none found" must be explained (wrong filter, event name changed after 7537eed, or evaluation stopped).
 d. newest 10 scan_symbols rows (symbol, status, reason, bar_ts).
 e. Engine PID 40964 command line and start time as one clean UTC timestamp, and the last startup events with ids.
 Say plainly whether the strategy is evaluating bars NOW. If not, find the cause (state, guard, terminal, code path) and report it. Do not restart without telling me.
2. If evaluation events are named differently since 7537eed, say what changed and make sure scripts/weekly_strategy_report (or equivalent) and the scorer still read the right events.
3. COMMIT scripts/forward_test_scorer.py, tests/test_scripts/ and the docs change in logical commits. Do not commit data/. Move the evidence from data/forward_test_report.md into docs/Stage2b_Live_Report.md and delete the data/ copy.
4. If the "MT5 timestamp discrepancy" observation mentioned in your report affects signals, bar times or expiry, describe it in two sentences.
5. git log --oneline -8, git status, last 15 lines of the full suite.
REPORT: one-page table (PROVEN / PROBABLE / TESTED-ONLY / UNPROVEN), raw output. Stop after this.