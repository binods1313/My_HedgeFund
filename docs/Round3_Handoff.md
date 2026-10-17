# Round 3 Handoff Report — 2026-10-16

**Session mode**: PAPER · **Branch**: `feature/echofi-ui` · **Engine PID**: 6204 (alive) · **Dashboard**: port 8502 LISTENING

---

## Four-Tier Status

The previous counts and `PROVABLE` label are withdrawn. Current B4 evidence:

| Tier        | Count | Items                                                            |
|-------------|-------|------------------------------------------------------------------|
| **PROVEN**     | 1     | B4 sleep/wake explanation                                        |
| **PROBABLE**   | 0     | None in this B4 correction                                        |
| **TESTED-ONLY**| 1     | Simulated gateway-watchdog tests                                 |
| **UNPROVEN**   | 1     | Whether any owner-thread call blocked during B4; not claimed as cause |

---

## Raw Evidence

### TE-01 — Engine alive, PAPER mode, journal path
```
PID 6204 is running (python, started 2026-10-16 04:02:09)
config.yaml: mode: PAPER, engine.db_path: data/engine_5057151780.db
Heartbeats: id=81271, ts=2026-10-16T01:11:48+00:00, pid=46628,
  connected=1, gateway_call_age=0.0001862s
```
→ Engine running on the code it loaded; brain NOT loaded on this branch.

### TE-02 — Journal schema v3

The block that used to sit here named `schema_migrations` and a v1 stamp of
`2026-10-13T18:12:11Z`. That table does not exist, and that stamp was not
read from the journal. The live table is `schema_version`. Command and
output: `docs/evidence/stage1_live_rows.md`.

```
(1, '2026-10-13T19:51:15+00:00')
(2, '2026-10-13T19:51:15+00:00')
(3, '2026-10-15T20:05:13+00:00')
```

### TE-03 — Earlier scan snapshot (superseded)

The counts 3856 / 481, the reason string `CYCLE_INTEVAL`, and the word
`SCANNED` were not reproduced from the journal. `scan_results.status` is
`recorded` or `skipped`. `scan_symbols.status` is lowercase `scanned` or
`skipped`. Rows with status `scanned` have an empty `reason`. That capture predates the accepted symbol correction and is historical. The
relevant scan result in the later reviewed journal is seven scanned and nine
`SYMBOL_NOT_FOUND`; the current configured names and post-restart results are
to be verified in the completion addendum in
`docs/Architect_Handoff_2026-10-15.md`.

### TE-04 — Events table (account = 23614, dominant)
```sql
SELECT category, COUNT(*) FROM events GROUP BY category ORDER BY count DESC;
-- account 23614, state 486, engine 16, gateway 16, telegram 12,
--   backup 11, command 4

SELECT COUNT(*) FROM events WHERE ts >= '2026-10-16T00:02:09';
-- 0
```
→ No new events since PID 6204 started (engine restarted cleanly).

### TE-05 — Equity flat, no trades

`equity_history` is not a table. The equity table is `equity_snapshots`.
The `SELECT DISTINCT equity FROM equity_history` line was not run. A later
read of `equity_snapshots` for 2026-10-15T22:34:08Z through 22:34:22Z shows
equity 99984.47 on every row in that window (see the handoff equity ledger).
`trades` exists. A count of trades was not saved with this correction.

### TE-06 — Dashboard port restored
```
run.bat:21: ...streamlit run app.py --server.port 8502 --server.headless true
netstat: TCP 0.0.0.0:8502 LISTENING
```
→ Port flag was missing from the original commit (33df3af); docs were aspirational. Now restored.

### TE-07 — MT5 close/reopen recovery (4s)
```
2026-10-15T22:34:14 INFO  gateway  terminal offline
2026-10-15T22:34:19 INFO  gateway  terminal recovered
```
→ 4-second downtime; 1 offline + 1 recovery event logged.

### TE-08 — MT5 auto-relaunch wiring
```
config.yaml → MT5_PATH env var → engine.config (line 280)
  → engine/__main__.py passes terminal_path to Gateway
  → engine/gateway.py:334-336: mt5.initialize(path=terminal_path)
```

### TE-09 — B4 sleep/wake (PROVEN)
```
B4 heartbeat gap: 2026-10-15 15:35:46Z → 17:08:53Z
  Windows Modern Standby: 2026-10-15 21:05:41 → 22:38:53 IST
  MT5 disconnect and re-authorization logs align with sleep and wake
```
→ The sleep/wake explanation is PROVEN. No owner-thread trace exists, so a
blocked call is not attributed to this gap. The watchdog's simulated stall
tests do not establish the cause of the live heartbeat gap.

### TE-10 — Brain isolation (parking on feature/brain-proposer)
```
On branch feature/echofi-ui:
  engine/brain.py does NOT exist (deleted)
  config.yaml has NO [brain] section
  __main__.py has no _maybe_run_brain, no brain import

On branch feature/brain-proposer:
  git show brain.py imports: json, math, re, dataclasses, typing,
    engine.keys — NO executor, NO gateway, NO send_order/place_order
  config.brain.enabled: false  (config.yaml line: "enabled: false")
  _maybe_run_brain() guards:
    - self.brain is None or self.config.brain is None → return
    - not brain_cfg.enabled → return
    - not self.gateway.connected → return
    - interval not elapsed → return
```
→ Brain is a PROPOSER only (returns HOLD on all failure paths).
Parked on its own branch; disabled by config; never loads on the active branch.

### TE-11 — Test suite

The three buckets below sum to 1586: test_engine 695, test_ui 332, root 559.
Secret scan 122, phase-0 28, and exec guards 35 are subsets of those buckets.
Adding them on top double-counts. This file does not contain the pytest
command that produced 1586. The measured run for this round is in the
session report, not in the 1586 line.

---

## Decisions
1. **Symbol proposal** → `docs/Symbol_Proposal_MetaQuotes.md` (see next section)
2. **Brain** → stays parked on `feature/brain-proposer`, `enabled: false`; do not merge to `feature/echofi-ui`
3. **Magic 234000** → acknowledged, not touched
4. **Migration backups** → `backups/archive/` + `backups/engine.db.acct-113363533.2026-10-12.archive` retained read-only

---

## Symbol Proposal (MetaQuotes-Demo)

Source: `docs/Live_Symbol_Survey_5057151780.md`

| Symbol   | trade_mode | Trade? |
|----------|------------|--------|
| XAUUSD   | 4 (FULL)   | YES    |
| XAUEUR   | 4 (FULL)   | YES    |
| XAUAUD   | 4 (FULL)   | YES    |

All other 54 symbols: DISABLED or absent. No `#` suffixes (plain MetaQuotes names).
Config add: `XAUAUD` to `trade.symbols`.
