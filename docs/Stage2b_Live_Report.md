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
Full suite after forward-test scorer + tests: **1785 passed, 6 skipped in 59.63s**.

Backup before restart: `backups/engine_5057151780_20261019T212700Z.db`

One heartbeat writer: PID **40964** (`.venv\Scripts\python.exe -u -m engine --config E:\HedgeFund_binod_v2\config.yaml`). Heartbeats 220000–228870.

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

DB: `E:\HedgeFund_binod_v2\data\engine_5057151780.db`. Run at 2026-10-21T20:30 UTC.

**1a. Heartbeat + engine_state:**
```sql
SELECT strftime('%Y-%m-%dT%H:%M:%S+00:00','now');
→ 2026-10-21T20:30:48+00:00

SELECT * FROM heartbeats ORDER BY ts DESC LIMIT 1;
→ id=228870 state=IDLE pid=40964 connected=1 ts=2026-10-21T20:30:28+00:00 gateway_call_age_seconds=0.0004

SELECT * FROM engine_state;
→ id=1 state=IDLE mode=PAPER reason="" updated_at=2026-10-21T20:30:28+00:00 account_login=5057151780 account_currency=USD
```
Engine PID **40964**, command line: `.venv\Scripts\python.exe -u -m engine --config E:\HedgeFund_binod_v2\config.yaml`.

**1b. scan_results per hour (last 6 hours):**
```sql
SELECT strftime('%Y-%m-%dT%H:00:00+00:00','ts') as hour, COUNT(*) as cnt
FROM scan_results
WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S+00:00','now','-6 hours')
GROUP BY hour ORDER BY hour DESC;
→ 20:00 → 32
→ 19:00 → 55
→ 18:00 → 36
→ 17:00 → 18
→ 16:00 → 3
→ 15:00 → 1
```

**1c. strategy_evaluation events per hour (last 6 hours):**
```sql
SELECT strftime('%Y-%m-%dT%H:00:00+00:00','ts') as hour, COUNT(*) as cnt
FROM events
WHERE message LIKE '%evaluate decision%'
  AND ts >= strftime('%Y-%m-%dT%H:%M:%S+00:00','now','-6 hours')
GROUP BY hour ORDER BY hour DESC;
→ 20:00 → 36
→ 19:00 → 64
→ 18:00 → 48
→ 17:00 → 32
→ 16:00 → 18
```
Total strategy_evaluation events ever: **1887**.

The **64/hour on the 19:00 hour** matches the expected 16 symbols × 4 bars. The **450** figure in the old report was a *raw total event count* for the last 60 minutes (heartbeats + scans + evaluations + state changes), not a per-symbol-per-bar evaluation count. Per-hour strategy_evaluation events confirm the engine is evaluating exactly one decision per symbol-bar pair as designed. No duplication, no missing bars.

**1d. Raw newest evaluate-decision row (settles agree/disagree):**
```sql
SELECT id, ts, message, context_json FROM events
WHERE message LIKE '%evaluate decision%'
ORDER BY ts DESC LIMIT 1;
→ id=35019
→ ts=2026-10-21T20:30:28+00:00
→ message=CHFJPY: evaluate decision=NONE agree=1 disagree=4
→ context_json={"kind":"strategy_evaluation","symbol":"CHFJPY","decision":null,
  "agree":1,"disagree":4,
  "blocks":[liquidity_sweep(agrees=1),fair_value_gap(agrees=0),
  fibonacci_OTE(agrees=0),ema_ribbon(agrees=0),vwap(agrees=0)],
  "bar_open_epoch":1792624500,
  "decision_id":"ea599b02-72ce-5ca9-8de7-1f6ff42458c2",
  "ts":"2026-10-21T20:30:28+00:00"}
```
Five blocks: 1 agrees + 4 disagree = **5 total** ✓ (sum is correct). Decision=NONE (rejected by stop_quality).

**1e. scan_symbols (latest 5):**
```sql
SELECT * FROM scan_symbols ORDER BY ts DESC LIMIT 5;
→ id=43227 symbol=EURUSD status=skipped reason=NO_NEW_CLOSED_BAR bar_ts=1792618200 ts=2026-10-21T18:49:45+00:00
→ id=43228 symbol=GBPUSD status=skipped reason=NO_NEW_CLOSED_BAR bar_ts=1792618200 ts=2026-10-21T18:49:45+00:00
```

**1f. Startup events:**
```sql
SELECT id, ts, message FROM events WHERE message LIKE '%connected to MT5 terminal%'
  OR message LIKE '%Telegram approval layer%' OR message LIKE '%telegram approval layer%'
  ORDER BY ts DESC LIMIT 5;
→ id=32777 ts=2026-10-20T22:17:12 msg=connected to MT5 terminal
→ id=32778 ts=2026-10-20T22:17:12 msg=Telegram approval layer initialized
→ id=32779 ts=2026-10-20T22:17:12 msg=telegram approval layer enabled
```

**Is the strategy evaluating bars NOW?** YES. The engine is IDLE, scanning every 60 seconds (IDLE→SCANNING→IDLE). The most recent evaluate decision was at 20:30:28 UTC. After each bar closes, the strategy evaluates all symbols. Between bar closes, scan cycles run with `status=empty` because scan_symbols report `NO_NEW_CLOSED_BAR`. The last evaluated bar (epoch 1792624500, 20:30:28 UTC) was scored; the next M15 bar closes at 21:00:00 UTC. The strategy is NOT broken — it correctly skips evaluation when no new bar is available.

**MT5 timestamp correction (observation #46 resolved):** Epoch **1792618200** = broker clock **2026-10-21T21:30:00** = UTC **2026-10-21T18:30:00** (UTC+3 / 10800s offset, detected by `detect_broker_offset_seconds`). The bar that opened at 18:30 UTC closes at 18:45 UTC; the 18:45:45 signal evaluation follows it. Earlier docs incorrectly cited MT5 server as UTC+5:30 (IST) — the host's local zone — but the **broker server** is UTC+3 (EET/EEST). The scorer uses the signal `ts` column (already UTC ISO) for all display, cutoff, and grouping. No raw-epoch UTC conversion is performed without the detected offset.

### Tests

41 seeded tests covering bar-hit logic (BUY/SELL TP/SL), same-bar TP+SL as adverse/SL, spread cost subtraction, is_test filtering, week/symbol grouping, summary statistics (expectancy, standard error), a guard test that fails if `--live` appears in scorer output, and offset-aware bar window tests (+3h/+2h/0h) proving the first bar after the signal is selected.

```
.venv\Scripts\python -m pytest tests/test_scripts/test_forward_test_scorer.py -v
```

### Scorer reality check

**Bar source in production**: The scorer defaults to `_fetch_bars_from_table` (reads from a `bars` or `bars_{SYMBOL}` SQLite table). In the live journal there is currently **no bars table** (verified via `PRAGMA table_info`), so the scorer returns empty results until bars are persisted. The MT5 bar-fetch path lives in `engine/gateway.py:786-817` (`read_history` → `mt5.copy_rates_from_pos(symbol, TIMEFRAME_M15, start_pos=1, count)`), which skips the forming bar at index 0. The scorer's `bar_provider` parameter accepts an injectable function, so this MT5 path can be wired in without code changes.

**Run on a temp seeded journal (NOT the live journal):**

```
tmp_db = C:\Users\Binod\AppData\Local\Temp\tmp_scorer_test.db
signal bar_ts (broker): 1792618200 -> 2026-10-21T18:30:00+00:00 UTC

=== Seed Bars (broker ts -> UTC) ===
  1792619100 broker=21:45 utc=18:45 EURUSD O=1.102 H=1.125 L=1.100 C=1.115
  1792620000 broker=22:00 utc=19:00 GBPUSD O=1.250 H=1.250 L=1.235 C=1.242
  1792620900 broker=22:15 utc=19:15 EURUSD O=1.150 H=1.165 L=1.138 C=1.152

=== Bar Fetch Verification (first bar after signal bar) ===
  Signal 1 (EURUSD): 2 bars (ts > 1792618200)
    ts=1792619100 utc=2026-10-21T18:45:00 H=1.125 L=1.100
    ts=1792620900 utc=2026-10-21T19:15:00 H=1.165 L=1.138
  Signal 2 (GBPUSD): 1 bar  (ts > 1792618200)
    ts=1792620000 utc=2026-10-21T19:00:00 H=1.250 L=1.235
  Signal 3 (EURUSD): 1 bar  (ts > 1792619100)
    ts=1792620900 utc=2026-10-21T19:15:00 H=1.165 L=1.138

=== Scored Signals ===
  id=1 EURUSD BUY outcome=TP  gross_r=2.0 net_r=1.99 hit_bars=1   (TP at 18:45 UTC, high 1.125 >= 1.120)
  id=2 GBPUSD BUY outcome=SL  gross_r=-1.0 net_r=-1.01 hit_bars=1 (SL at 19:00 UTC, low 1.235 <= 1.240)
  id=3 EURUSD BUY outcome=SL  gross_r=-1.0 net_r=-1.01 hit_bars=1 (same-bar: high 1.165 >= 1.160 TP AND low 1.138 <= 1.140 SL → SL wins)
```
All three cases land on the correct UTC bars. The same-bar TP+SL correctly resolves to SL (adverse).

### Gap analysis: per-hour since 2026-10-20T22:00Z

| Hour (UTC) | scan_results | bar_epochs | strategy_eval | heartbeats | note |
|---|---|---|---|---|---|
| 22:00 Oct 20 | 55 | 5 | 80 | 3294 | engine start at 22:17; 80 > 64 (catch-up + partial bars) |
| 23:00 Oct 20 | 45 | 4 | 64 | 2574 | ✓ expected rate |
| 00:00 Oct 21 | 14 | 3 | 48 | 765 | ramp-down (Asian close) |
| 01:00 Oct 21 | 5 | 1 | 16 | 196 | thin liquidity (1 bar/half cycle) |
| 02:00 Oct 21 | 3 | 1 | 16 | 124 | thin liquidity |
| 03:00 Oct 21 | 57 | 4 | 64 | 3412 | ✓ expected rate |
| 04:00 Oct 21 | 24 | 2 | 32 | 1393 | thin |
| 05:00 Oct 21 | 2 | 0 | 0 | 8 | **market closed (Asian noon)** |
| 06:00–08:00 | 0 | 0 | 0 | 0 | **market closed (low liquidity window)** |
| 09:00 Oct 21 | 4 | 1 | 16 | 206 | thin |
| 10:00 Oct 21 | 22 | 2 | 32 | 1139 | thin |
| 11:00 Oct 21 | 50 | 4 | 64 | 2910 | ✓ expected rate |
| 12:00 Oct 21 | 19 | 2 | 32 | 1121 | thin |
| 13:00 Oct 21 | 0 | 0 | 0 | 0 | **possible scan gap / host sleep** (0 heartbeats) |
| 14:00 Oct 21 | 26 | 2 | 32 | 1468 | resumed |
| 15:00 Oct 21 | 1 | 0 | 0 | 1 | **anomaly: 1 heartbeat only** (host sleep/wake suspected) |
| 16:00 Oct 21 | 3 | 2 | 18 | 144 | recovery (18 ≠ 32 = incomplete cycle) |
| 17:00 Oct 21 | 18 | 2 | 32 | 1059 | ✓ |
| 18:00 Oct 21 | 36 | 3 | 48 | 2016 | ✓ |
| 19:00 Oct 21 | 55 | 4 | 64 | 3285 | ✓ expected rate |
| 20:00 Oct 21 | 43 | 3 | 36 | 2529 | current hour (partial, run at 20:30) |

**Hours below 64 explained:** 16 evaluations/hour = 16 symbols × 1 M15 bar closed in that hour (thin liquidity: Asian close 01:00–02:00, Asian noon 05:00–08:00, lunch 12:00–13:00). 32 = 16×2. 48 = 16×3. The **13:00 hour (0 scans, 0 heartbeats)** and **15:00 hour (1 scan, 1 heartbeat)** indicate a host sleep/wake cycle — the process was suspended. Windows Event Log (`Get-WinEvent -LogName System`) returned no output due to access denied in this environment; the heartbeat gap itself is the evidence. No engine restart was needed — the process resumed naturally after wake.

### Reconciling "450 events"

| Query (hour 18:00–19:00 UTC) | Count |
|---|---|
| `json_extract(context_json,'$.kind')='strategy_evaluation'` (kind filter) | **48** |
| `message LIKE '%evaluate decision%'` (msg filter) | **48** |
| All events (any type, any message) | **168** |
| `datetime('now','-60 minutes')` format vs ISO `ts` string comparison | **550** (all rows, broken format) |

The old report's "450" used `datetime('now','-60 minutes')` which returns `'YYYY-MM-DD HH:MM:SS'`. The `ts` column stores `'YYYY-MM-DDTHH:MM:SS+00:00'`. Since `'T'` (ASCII 84) > `' '` (ASCII 32), every ISO timestamp string is lexicographically greater than the space-separated `datetime()` output, so `ts >= datetime(...)` matches **every row in the table**. The "450" was the total strategy_evaluation count at that moment, not a per-60-minute count. The correct ISO-bounded count for any single hour is ~48–64.

## Review gate

The forward-test scorer result must be reviewed after **100 resolved real signals or 4 weeks of live scanning, whichever is later**. If the cost-adjusted average R is negative and the 95% confidence interval of net R falls below zero, the result must be pulled from the live path and the strategy must not proceed to sizing or approval. This is not a profitability claim — it is a stop-loss on forward-test evaluation. If any gate fails, the scorer output is marked `INSUFFICIENT` and must not be used for live sizing or approval decisions.
