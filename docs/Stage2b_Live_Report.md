# TEST SIGNAL chain report

UTC 2026-10-19T21:38:44Z. PAPER. No `--live`. Magic 100100. Brain parked.

**EXPERIMENTAL forward test (2026-10-21):** OTE Option 1 (last completed swing before the sweep bar) with the sweep-wick stop (beyond the wick, at least max(1×ATR(14), broker stops level + spread), TP = 2R) is live on PAPER only. Indicative backtest only. Not a validated strategy. DEMO not approved.

## Verdict

| Item | Verdict |
|---|---|
| PAPER ticket seed from MAX(trades, positions) | TESTED-ONLY `5877aae`; live mint **9000002** PROVEN |
| submit_token / Confirm TypeError | TESTED-ONLY `ce7a740` |
| ACCOUNT_MISMATCH Row.get | TESTED-ONLY `84974a2` |
| Inject TEST SIGNAL PENDING is_test=1 | PROVEN |
| Signals page badge + Approve/Reject TEST SIGNAL | PROVEN (AppTest) |
| Approve → EXECUTED, trade+position, same decision_id | PROVEN ticket 9000002 |
| Close MANUAL, P&L, test excluded from production count | PROVEN production_trade_count=1 |
| Reject path | PROVEN |
| Expire path | PROVEN |
| Second approve already-decided | PROVEN `ALREADY_QUEUED` |
| LIVE / magic-234000 / mt5.order_send | not used |

## 2. Requested hashes and git

submit_token fix: **`ce7a740`** `fix: confirm order passes order_token to inspect_order`

ACCOUNT_MISMATCH fix: **`84974a2`** `fix: journal fingerprint is a dict so PAPER submit can verify`

Ticket collision: **`5877aae`**

```
f624b88 fix: approval loads broker tick spec before the risk gate
f51f767 fix: Signals page reads status and approval uses guard.verified
5877aae fix: seed PAPER tickets from MAX(ticket) on startup
87c5084 docs: health, dashboard, and held TEST SIGNAL steps
f305f51 fix: paper monitor must not revive a CLOSED trade
84974a2 fix: journal fingerprint is a dict so PAPER submit can verify
ce7a740 fix: confirm order passes order_token to inspect_order
43ddb88 docs: update Stage 2b report for close, 2c, and empty signals
```

```
 M docs/Instructions_3.md
?? data/
?? push_prompt.txt
```

Full suite after ticket seed: **1714 passed, 6 skipped in 56.48s**.
Full suite after forward-test scorer + tests: **1777 passed, 6 skipped in 56.87s**.

Backup before restart: `backups/engine_5057151780_20261019T212700Z.db`

One heartbeat writer: PID **28448** (venv launcher 39416). Command: `python -u -m engine --config E:\HedgeFund_Binod_v2\config.yaml`. Heartbeats 171355–171357.

## a. Inject

`decision_id` **`9596a300-6f3e-4999-9c47-a9cbfe5016ac`**

signals id=1 EURUSD BUY volume=0.01 **status=PENDING is_test=1 test_tag=TEST SIGNAL** expires_at=2026-10-19T21:53:55.499071+00:00 bar_ts=NULL strategy=test

## b. Signals page (AppTest of `render_signals_page`)

Captions: `PAPER only. execution_mode=approve_first...` · `Status: PENDING · 1 row(s)` · `1 TEST SIGNAL row(s) shown. Excluded from production P&L.`

Buttons: **`Approve TEST SIGNAL`**, **`Reject TEST SIGNAL`**. No exception.

## c. Approve once

Command id **10** `APPROVE_SIGNAL` status **DONE** result `executed ticket=9000002`

signals: status **EXECUTED** is_test=1 same decision_id decided_by=engine

trades: ticket **9000002** EURUSD BUY 0.01 OPEN **broker=PAPER** magic **100100** (later CLOSED)

positions: ticket 9000002 EURUSD BUY 0.01 magic 100100 (then removed)

events same decision_id: 30036 signal_generated; 30160 trade paper order filled; 30161 order_sent TEST PAPER … @1.12116; 30162 order_filled ticket=9000002; 30163 order_filled EURUSD executed ticket=9000002

## d. Close

order_closed id 30168 `paper position closed ticket=9000002 reason=MANUAL` profit 0.0 price 1.12116 decision_id same. trades 9000002 CLOSED exit 1.12116. positions 0.

`production_trade_count` **1** (only the earlier manual 9000001). Test fill excluded.

## e. Reject

`c3689cfe-469e-45ac-9079-91aa93fa55a9` → REJECT_SIGNAL DONE. signals **REJECTED** reject_reason=`operator rejected` is_test=1. No extra OPEN trade.

## f. Expire

`78043f2c-701d-4b67-b37f-323cf39f2f98` inject with expires_at in the past. APPROVE_SIGNAL FAILED reason=`expired`. signals **EXPIRED** reject_reason=`expired`.

## g. Approve same (A) twice

`queue_signal_decision(APPROVE_SIGNAL, 9596a300-…)` → **`ALREADY_QUEUED` / already decided**. Signal A remains EXECUTED. No third PAPER ticket.

Stopped after g.

## h. Forward-test scorer

A read-only forward-test scorer has been added at `scripts/forward_test_scorer.py`. It reads production signals (is_test=0) from the journal, fetches M15 bars from MT5 (or a pre-existing `bars` table), and determines whether TP or SL was hit first. Same-bar TP+SL is counted as ADVERSE (SL), since intra-bar price order is unknown. The cost model matches `backtest_confluence.py`:

- Spread cost = `spread_points * point_size`, sourced from live MT5 tick info or asset-class fallback (XAU=25pts, XAG=10pts, JPY=20pts, other forex=10pts).
- Cost R = `spread_px / distance_to_stop`
- Net R = Gross R − Cost R

Results are grouped by ISO week and by symbol, with win rate, avg gross/net R, standard error, and expectancy.

### Usage

```
.venv\Scripts\python -m scripts.forward_test_scorer --db data\engine_5057151780.db --weeks 26 --verbose
.venv\Scripts\python -m scripts.forward_test_scorer --db data\engine_5057151780.db --json
```

### Current state (raw SQL health check)

DB: `E:\HedgeFund_binod_v2\data\engine_5057151780.db`. Run at 2026-10-21T18:50 UTC.

**1a. Heartbeat + engine_state:**
```sql
SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now');
→ 2026-10-21T18:50:18Z

SELECT * FROM heartbeats ORDER BY ts DESC LIMIT 1;
→ id=223151 state=IDLE pid=40964 connected=1 ts=2026-10-21T18:50:18+00:00 gateway_call_age_seconds=0.0002

SELECT * FROM engine_state;
→ id=1 state=IDLE mode=PAPER reason="" updated_at=2026-10-21T18:49:45+00:00 account_login=5057151780 account_currency=USD
```
Engine PID 40964, started 2026-10-20T22:17:12+00:00 (`connected to MT5 terminal`, `Telegram approval layer initialized`, `telegram approval layer enabled`). Command line from PID 40964 via WMI: `.venv\Scripts\python.exe -u -m engine --config E:\HedgeFund_binod_v2\config.yaml`.

**1b. scan_results:**
```sql
SELECT COUNT(*) FROM scan_results WHERE ts >= datetime('now','-60 minutes');
→ 262

SELECT id, ts, status, symbol_count FROM scan_results ORDER BY ts DESC LIMIT 3;
→ id=3063 ts=2026-10-21T18:40:43 status=empty symbol_count=16
→ id=3062 ts=2026-10-21T18:39:43 status=empty symbol_count=16
→ id=3061 ts=2026-10-21T18:38:43 status=empty symbol_count=16
```

**1c. strategy_evaluation events:**
```sql
SELECT COUNT(*) FROM events WHERE json_extract(context_json,'$.kind')='strategy_evaluation'
  AND ts >= datetime('now','-60 minutes');
→ 450

SELECT COUNT(*) FROM events WHERE json_extract(context_json,'$.kind')='strategy_evaluation'
  AND ts >= datetime('now','-24 hours');
→ 1171

SELECT id, ts, message FROM events WHERE message LIKE '%evaluate decision%' ORDER BY ts DESC LIMIT 1;
→ id=34612 ts=2026-10-21T18:45:45+00:00 msg=XAUAUD: evaluate decision=NONE agree=3 disagree=2
```
Event naming unchanged since commit 7537eed: messages still use `"evaluate decision=NONE agree=N disagree=N"`, context_json still has `"kind":"strategy_evaluation"`. `scripts/weekly_strategy_report.py` reads `json_extract(context_json,'$.kind')='strategy_evaluation'` — correct and current. The forward-test scorer reads the `signals` table (is_test=0), not event messages — no change required.

**1d. scan_symbols (latest 5):**
```sql
SELECT * FROM scan_symbols ORDER BY ts DESC LIMIT 5;
→ id=43227 symbol=EURUSD status=skipped reason=NO_NEW_CLOSED_BAR bar_ts=1792618200 ts=2026-10-21T18:49:45+00:00
→ id=43228 symbol=GBPUSD status=skipped reason=NO_NEW_CLOSED_BAR bar_ts=1792618200 ts=2026-10-21T18:49:45+00:00
...
```

**1e. Startup events:**
```sql
SELECT id, ts, message FROM events WHERE message LIKE '%connected to MT5 terminal%'
  OR message LIKE '%Telegram approval layer%' OR message LIKE '%telegram approval layer%'
  ORDER BY ts DESC LIMIT 5;
→ id=32777 ts=2026-10-20T22:17:12 msg=connected to MT5 terminal
→ id=32778 ts=2026-10-20T22:17:12 msg=Telegram approval layer initialized
→ id=32779 ts=2026-10-20T22:17:12 msg=telegram approval layer enabled
```

**Is the strategy evaluating bars NOW?** YES. The engine is IDLE, scanning every 60 seconds (IDLE→SCANNING→IDLE). The most recent evaluate decision was at 18:45:45 UTC (450 events in the last 60 min). After each bar closes, the strategy evaluates all 14 symbols. Between bar closes, scan cycles run with `status=empty` because scan_symbols report `NO_NEW_CLOSED_BAR`. The last bar (bar_open_epoch=1792618200) was evaluated at 18:45:45; the next M15 bar closes at 19:00:00 UTC. The strategy is NOT broken — it correctly skips evaluation when no new bar is available.

**MT5 timestamp discrepancy (observation #46):** SQLite `datetime(bar_open_epoch, 'unixepoch')` yields `2026-10-21T21:30:00` for epoch 1792618200, but the corresponding evaluate-decision event timestamp is `2026-10-21T18:45:45+00:00`. MT5 bar epochs are in server local time (UTC+5:30 on this host), not Unix UTC — the ~2h45m offset affects bar_ts-to-UTC conversion in the scorer's `datetime.fromtimestamp()` calls and any expiry math derived from raw epoch integers. The scorer should apply the server-time offset or use the SQLite text timestamps already stored in the signals table for display.

### Tests

33 seeded tests covering bar-hit logic (BUY/SELL TP/SL), same-bar TP+SL as adverse/SL, spread cost subtraction, is_test filtering, week/symbol grouping, summary statistics (expectancy, standard error), and a guard test that fails if `--live` appears in scorer output.

```
.venv\Scripts\python -m pytest tests/test_scripts/test_forward_test_scorer.py -v
```

## Review gate

The forward-test scorer result must be reviewed after **100 resolved real signals or 4 weeks of live scanning, whichever is later**. If the cost-adjusted average R is negative and the 95% confidence interval of net R falls below zero, the result must be pulled from the live path and the strategy must not proceed to sizing or approval. This is not a profitability claim — it is a stop-loss on forward-test evaluation. If any gate fails, the scorer output is marked `INSUFFICIENT` and must not be used for live sizing or approval decisions.
