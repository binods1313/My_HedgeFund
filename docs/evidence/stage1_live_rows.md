# Stage 1 live rows

The previous copy of this file showed `scan_results` columns `bid`, `ask`,
`confidence`, and `signal`, status `SCANNED`, cycle id `20261015-143706`,
and prices EURUSD 1.16523, GBPUSD 1.31542, USDJPY 149.312. Those rows were
not read from the journal. `scan_results` has never had those columns.
`quotes` has no such prices. Counts 208 and 1664 were not produced by a
saved query.

Command (read-only, `mode=ro`):

```
.\.venv\Scripts\python.exe C:\Users\Binod\AppData\Local\Temp\hf_stage1_ro.py
```

The script opens `file:E:/HedgeFund_Binod_v2/data/engine_5057151780.db?mode=ro`
and runs the SQL printed below. Output captured 2026-10-16:

```
SQL
PRAGMA table_info(scan_results)
(0, 'id', 'INTEGER', 0, None, 1)
(1, 'symbols_json', 'TEXT', 1, None, 0)
(2, 'ranked_json', 'TEXT', 1, "'[]'", 0)
(3, 'ts', 'TEXT', 1, None, 0)
(4, 'cycle_id', 'TEXT', 0, None, 0)
(5, 'status', 'TEXT', 0, None, 0)
(6, 'reason', 'TEXT', 0, None, 0)
(7, 'symbol_count', 'INTEGER', 0, None, 0)
rows 8

SQL
PRAGMA table_info(scan_symbols)
(0, 'id', 'INTEGER', 0, None, 1)
(1, 'cycle_id', 'TEXT', 1, None, 0)
(2, 'symbol', 'TEXT', 1, None, 0)
(3, 'status', 'TEXT', 1, None, 0)
(4, 'reason', 'TEXT', 1, "''", 0)
(5, 'bar_ts', 'TEXT', 0, None, 0)
(6, 'ts', 'TEXT', 1, None, 0)
rows 7

SQL
PRAGMA table_info(commands)
(0, 'id', 'INTEGER', 0, None, 1)
(1, 'command', 'TEXT', 1, None, 0)
(2, 'params_json', 'TEXT', 1, "'{}'", 0)
(3, 'status', 'TEXT', 1, "'PENDING'", 0)
(4, 'result', 'TEXT', 0, None, 0)
(5, 'reason', 'TEXT', 0, None, 0)
(6, 'dedupe_key', 'TEXT', 0, None, 0)
(7, 'ts', 'TEXT', 1, None, 0)
(8, 'claimed_at', 'TEXT', 0, None, 0)
(9, 'finished_at', 'TEXT', 0, None, 0)
rows 10

SQL
SELECT version, applied_at FROM schema_version ORDER BY version
(1, '2026-10-13T19:51:15+00:00')
(2, '2026-10-13T19:51:15+00:00')
(3, '2026-10-15T20:05:13+00:00')
rows 3

SQL
SELECT 'scan_results', COUNT(*) FROM scan_results
    UNION ALL SELECT 'scan_symbols', COUNT(*) FROM scan_symbols
    UNION ALL SELECT 'quotes', COUNT(*) FROM quotes
('scan_results', 707)
('scan_symbols', 5664)
('quotes', 1)
rows 3

SQL
SELECT status, COUNT(*) FROM scan_results GROUP BY status ORDER BY status
('recorded', 354)
('skipped', 353)
rows 2

SQL
SELECT status, reason, COUNT(*) FROM scan_symbols GROUP BY status, reason ORDER BY 3 DESC
('skipped', 'MARKET_CLOSED', 3186)
('scanned', '', 2477)
('skipped', 'NO_NEW_CLOSED_BAR', 1)
rows 3

SQL
SELECT bid, ask, confidence, signal FROM scan_results LIMIT 1
ERROR no such column: bid

SQL
SELECT COUNT(*) FROM quotes
    WHERE bid IN (1.16523, 1.16537, 1.31542, 1.31561, 149.312, 149.326)
       OR ask IN (1.16523, 1.16537, 1.31542, 1.31561, 149.312, 149.326)
(0,)
rows 1

SQL
SELECT symbol, bid, ask, spread, tick_time, updated_at FROM quotes
('ACCOUNT_EQUITY', 99984.47, 99984.47, 0.0, 1792118520, '2026-10-16T02:42:00+00:00')
rows 1

SQL
SELECT id, cycle_id, ts, status, reason, symbol_count
    FROM scan_results ORDER BY id DESC LIMIT 3
(707, '98c4840dec54465495d1374668d1a55f', '2026-10-16T02:41:40+00:00', 'skipped', 'CYCLE_INTERVAL', 0)
(706, '9f2dc26f4b0d4049b8e481794e386187', '2026-10-16T02:41:39+00:00', 'recorded', '', 16)
(705, '7213cf14bf374470afa5536ff9b61b32', '2026-10-16T02:40:39+00:00', 'skipped', 'CYCLE_INTERVAL', 0)
rows 3

SQL
SELECT COUNT(*) FROM scan_results
    WHERE cycle_id = '20261015-143706' OR status = 'SCANNED'
(0,)
rows 1
```

`commands` exists on this file. Its columns match migration 1 in
`engine/journal.py` (`CREATE TABLE IF NOT EXISTS commands` in `MIGRATION_1`).
Status values on `scan_symbols` are lowercase `scanned` and `skipped`.
There is no `SCANNED` status. The newest `scan_results` rows above were
written by the process that was already running; `CYCLE_INTERVAL` rows
stop only after that process is restarted onto the skip-reason fix.
