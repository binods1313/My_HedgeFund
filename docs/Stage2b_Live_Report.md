# TEST SIGNAL chain report

UTC 2026-10-19T21:38:44Z. PAPER. No `--live`. Magic 100100. Brain parked.

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
