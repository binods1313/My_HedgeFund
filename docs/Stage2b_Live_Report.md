# Stage 2c health and dashboard report

UTC of this check: 2026-10-19T17:41:10+00:00. PAPER. No `--live`. Magic 100100. Brain parked. TEST SIGNAL not run. I did not place a BUY (step 3 is hold-until-go).

## Verdict

| Item | Verdict |
|---|---|
| Engine heartbeating after 04:17Z evidence | PROVEN (then restarted to apply a close bugfix) |
| Newest heartbeat before restart | PROVEN id 166558 at 17:31:40Z, PID 30224 |
| Backup before last restart | PROVEN `backups/engine_5057151780_20261019T174035Z.db` |
| Restart PAPER, no `--live`, PID 7660 still alive | PROVEN |
| localhost:8502 serving | PROVEN HTTP 200 / `_stcore/health` ok |
| Signals page empty sentence + PAPER disclaimer, no exception | PROVEN (AppTest of live `app.py`) |
| Journal + status card current values | PROVEN (AppTest) |
| No groq/openrouter/openai/anthropic/claude/ollama on those pages | PROVEN |
| Manual PAPER BUY+Close this session by me | UNPROVEN (no go) |
| Operator BUY+Close ticket 9000001 journal chain | PROVEN (read-only SQL) |
| Engine monitor resurrected CLOSED 9000001 until restart | PROVEN; fix `f305f51` TESTED-ONLY |
| TEST SIGNAL injector run live | UNPROVEN (held) |

## 1. Health

Command: read-only sqlite on `data/engine_5057151780.db`; `Get-CimInstance Win32_Process`.

At 17:31:41Z the old process was still alive: heartbeat **166558**, ts **2026-10-19T17:31:40+00:00**, pid **30224**, state IDLE, connected=1. Command line:

`E:\HedgeFund_Binod_v2\.venv\Scripts\python.exe -u -m engine --config E:\HedgeFund_Binod_v2\config.yaml`

Trades said 9000001 CLOSED, but `positions` still had ticket 9000001 (engine `on_tick` re-upserted it). Backup then restart to load `f305f51`.

Backup: `E:\HedgeFund_Binod_v2\backups\engine_5057151780_20261019T174035Z.db`

New interpreter **PID 7660**. Log: mode=PAPER, magic=100100, account MATCH ****1780 USD MetaQuotes-Demo. Heartbeat 167113 at 17:41:10Z. Positions count **0**. Trade 9000001 remains CLOSED PAPER.

## 2. Dashboard (http://localhost:8502)

`Invoke-WebRequest http://localhost:8502` → 200. `http://127.0.0.1:8502/_stcore/health` → `ok`.

agent-browser Chrome exited (`CDP response channel closed` / `about:blank`). Pages were driven with Streamlit AppTest against live `app.py` (same UI code the server runs).

**Signals:** no exception, no `st.error`. Caption: `PAPER only. execution_mode=approve_first. Approve and Reject queue a command; they do not place an order.` Empty: `No signals with status PENDING recorded yet.` One warning: gain/loss hues 0° apart (design, not a crash). Vendor list empty.

**Journal:** `SYSTEM STATUS · READ-ONLY JOURNAL VIEW`, path `E:\HedgeFund_Binod_v2\data\engine_5057151780.db`, `No signals recorded yet. An empty table is a fact about the journal, not an error.` No errors. Vendor list empty.

**Exchange / status card:** Bid/Ask EURUSD 1.12127/1.12128, balance/equity $99,984.47, open P&L $0.00, last signal —, execution_mode=approve_first on the card, heartbeat shown as running. Caption mentions Finviz as an external browser link (not a brain vendor). No groq/openrouter/openai/anthropic.

## 3. Manual BUY

Not run by me. Operator ticket **9000001** from the journal (read-only):

`trades`: ticket 9000001 EURUSD BUY 0.01 entry 1.1209 exit 1.12106 status CLOSED magic 100100 broker PAPER ts 2026-10-19T17:20:15Z updated 17:24:48Z.

Events / lifecycle, decision_id `True:EURUSD`:

| id | ts | category | message |
|---|---|---|---|
| 29572 | 17:20:15Z | trade | paper order filled |
| 29573 | 17:20:15Z | order_sent | paper order sent EURUSD BUY @1.1209 |
| 29574 | 17:20:15Z | order_filled | paper order filled EURUSD ticket=9000001 |
| 29583 | 17:24:48Z | trade | paper position closed |
| 29584 | 17:24:48Z | order_closed | paper position closed ticket=9000001 reason=MANUAL |

After restart, `positions` is empty.

## 4. TEST SIGNAL chain (prepared, not run)

Do not invoke until you say go. PAPER only. Engine must stay without `--live`.

1. Confirm `load_engine_config().mode == "PAPER"`. If LIVE, `inject_test_signal` raises before any write.
2. From project venv, in a one-shot that is **not** the engine loop:

```
from datetime import datetime, timedelta, timezone
from engine.config import load_engine_config
from engine.journal import Journal
from engine.test_signal import inject_test_signal
cfg = load_engine_config()
j = Journal(cfg.db_path, migrate=False)
exp = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
did = inject_test_signal(mode=cfg.mode, journal=j, symbol="EURUSD",
    direction="BUY", entry=<ask>, stop_loss=<below>, take_profit=<above>,
    volume=0.01, expires_at=exp)
print(did); j.close()
```

Expected immediately: one `signals` row `PENDING`, `is_test=1`, `test_tag=TEST SIGNAL`, `decision_id=did`, `expires_at` set, `bar_ts` NULL. Lifecycle `signal_generated`. **Zero** `trades` / `positions`.
3. Dashboard Signals → Pending: badge TEST SIGNAL. Approve queues `APPROVE_SIGNAL` only (or Telegram `YES <did>` / `APPROVE <did>` from the authorised chat).
4. Engine next loop: handler re-checks expiry, drift, risk, market, account, PAPER, not paused; `PaperExecutor` fill. Expected: signal `EXECUTED`; `trades` PAPER 9xxxxxx OPEN; `positions` row; events `order_sent` / `order_filled` with the **same** `decision_id`; `is_test` still 1 on the signal. Production P&L helper `production_trade_count` stays 0.
5. Close from Positions: `order_closed` reason MANUAL, same `decision_id`. Do not use TEST SIGNAL P&L in production stats.

Reject instead of approve: signal `REJECTED`, no trade.

## Git / suite

Latest code: `f305f51` monitor ghost fix. Dashboard AppTest was not a full pytest run. Last full suite on this branch: 1709 passed, 6 skipped (before `f305f51` / `84974a2` / `ce7a740`). Monitor module: 3 passed.
