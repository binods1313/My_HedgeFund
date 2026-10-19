# Stage 2b live report

UTC of the live check: 2026-10-19T03:17:06+00:00. Branch `feature/echofi-ui`. PAPER. No `--live`. Magic 100100. Magic 234000 was not used. `execution_mode` is absent in `config.yaml` and loads as `approve_first`. `auto_execute` is rejected. The brain was not started. No order was sent.

## Verdict

| Item | Verdict |
|---|---|
| Closed-bar open is what `scan_symbols.bar_ts` stores | PROVEN |
| Snapshot `tick.time` is not an M15 open and is not the stored bar | PROVEN |
| `decision_id` inputs are `symbol\|confluence\|bar_open_epoch` | PROVEN |
| Broker offset +10800s (UTC+3) on the 03:07:44 tick | PROVEN |
| Engine accepted an offset (no `BROKER_OFFSET_UNSAFE`) | PROVEN |
| Expiry formula `min(bar_close_utc+900s, created_utc+1800s)`, no skew fallback | TESTED-ONLY |
| Live `signals.expires_at` | UNPROVEN (no signal row; every symbol decided NONE) |
| One cycle of `evaluate()` after the 03:15 UTC close, five blocks | PROVEN |
| PENDING row, idempotent re-run, supersede, zero orders | TESTED-ONLY |
| `Engine.read_history` / `evaluate_strategy` exist and the stored function is called | TESTED-ONLY |
| Research Desk 01–08 read "Not running yet (planned)" | TESTED-ONLY |
| Manual Exchange BUY path (read from code, not clicked) | PROBABLE |
| Restart: one PAPER process, same PID still alive later, heartbeats advancing | PROVEN |
| Stage 2c | Not started. TEST SIGNAL injector is held. No config change applied for it. |

Unit tests are TESTED-ONLY even when they pass.

## Git

Commands: `git log --oneline -12`, `git status --short`, `git branch --show-current`.

```
50bf9a5 fix: call the strategy function the engine stores
dda2687 fix: research desk cards say not running yet
2172f19 fix: use the closed M15 bar open and a UTC expiry
5b8c633 docs: finalize Stage 2b report with final restart evidence
b344dd8 docs: update Stage 2b report with live evidence (+3h broker skew, signal path wired)
9a80177 config: enable ai_signals_enabled for Stage 1b signal wiring
1d8b684 docs+test: refine Stage 2b instructions and expiry skew test docstring
8c364de feat: wire Engine.read_history/evaluate_strategy/account + expiry skew guard
c3d4302 test: add broker clock skew tests to signal expiry
b59d1b1 feat: wire Stage 1b signal path into Engine class
10cebe9 feat: add signals page UI component
77abeed test: cover signal expiry, volume caps, gateway history, import guard
```

```
feature/echofi-ui
 M docs/Instructions_3.md
?? .kilo/
?? data/
?? push_prompt.txt
```

`Instructions_3.md`, `.kilo/`, `data/`, and `push_prompt.txt` are not part of these commits.

`config.yaml` was already committed in `9a80177`. The scanner flag is the only change:

```
scanner:
-  ai_signals_enabled: false
+  ai_signals_enabled: true
```

There is no top-level `ai_signals_enabled`. The loader was still reading the top-level key, so the flag came out false. `2172f19` reads `scanner.ai_signals_enabled` first. That is TESTED-ONLY (`test_scanner_ai_signals_enabled_loads_without_a_top_level_key`).

## Bar time

The old stored field is `scan_symbols.bar_ts`. It held the snapshot tick epoch (`tick.time`), not a bar open. Last such rows before the fix, id 25120 XAUAUD `1792390009`. That epoch mod 900 is 409. Broker clock 06:06:49. It does not end at :00, :15, :30, or :45.

The signal path now stores the history frame's last `time`, which is the closed M15 bar open (`copy_rates_from_pos` start_pos=1). The same integer is the `decision_id` input and the idempotency key. The snapshot tick is not written on that path.

One-shot read at 2026-10-19T03:07:42Z, EURUSD, then `mt5.shutdown()`:

| | |
|---|---|
| Tick epoch | 1792390064 (mod 900 = 464) |
| Tick broker clock | 2026-10-19 06:07:44 |
| Closed bar open epoch | 1792388700 (mod 900 = 0) |
| Broker time | 2026-10-19 05:45:00 |
| UTC (epoch − 10800) | 2026-10-19 02:45:00 |
| IST (UTC+5:30) | 2026-10-19 08:15:00 |
| decision_id inputs | `EURUSD\|confluence\|1792388700` |

The next close, scanned at 03:15:35Z, stored open epoch `1792389600` (mod 900 = 0): broker 06:00:00, UTC 03:00:00, IST 08:30:00. Event 28757 context: `decision_id_inputs=EURUSD|confluence|1792389600`, `decision_id=04d0d8d7-42c3-573d-8315-5357b06cf8eb`.

`tests/test_engine/test_signal_e2e.py::test_unaligned_timestamp_is_not_used` fails if 05:17:42 is stored or hashed. TESTED-ONLY.

## Expiry

`detect_broker_offset_seconds` takes fresh tick epochs minus real UTC, rounds to the nearest hour, and accepts the hour only when the leftover is at most 120 seconds and the hour is inside −12..+14. A half-hour offset or a stale tick returns None. The process keeps a new reading only when it is the first, the same as the last, or exactly ±3600 seconds (DST). Any other jump is refused and the previous offset stays. A restart is how a real multi-hour change is taken up. `broker_offset_seconds` on the cycle, including 0, is an override and skips detection.

If the offset is None the cycle skips `BROKER_OFFSET_UNSAFE`, does not consume the bar, and does not invent `created+1800`. When the offset is known, broker open minus offset is the UTC open, the close is 15 minutes later, and expiry is `min(bar_close_utc + 900s, created_utc + 1800s)`. No weekend clamp. No skew fallback.

Tests in `tests/test_engine/test_signal_expiry.py`: +3h, +2h, 0h, Friday 20:45 UTC open expires 21:15 UTC, before Saturday and before Monday. TESTED-ONLY. No live `expires_at` exists: `signals` count is 0.

## Strategy, one cycle after the 03:15 UTC close

Command was a read-only SQL query of `events` where `message` contains `evaluate decision` and `ts >= 2026-10-19T03:15:00`. All sixteen are decision NONE. No PENDING row. The following cycle at 03:16:35 skipped `NO_NEW_CLOSED_BAR` and still stored `1792389600`.

| id | symbol | agree | disagree | blocks |
|---|---|---|---|---|
| 28757 | EURUSD | 2 | 3 | sweep AGREE, fvg AGREE, fib OPPOSE, ema OPPOSE, vwap OPPOSE |
| 28759 | GBPUSD | 2 | 3 | sweep AGREE, fvg AGREE, fib OPPOSE, ema OPPOSE, vwap OPPOSE |
| 28761 | USDJPY | 2 | 3 | sweep AGREE, fvg AGREE, fib OPPOSE, ema OPPOSE, vwap OPPOSE |
| 28763 | USDCAD | 1 | 4 | sweep AGREE, fvg OPPOSE, fib OPPOSE, ema OPPOSE, vwap OPPOSE |
| 28765 | USDCHF | 3 | 2 | sweep AGREE, fvg AGREE, fib OPPOSE, ema OPPOSE, vwap AGREE |
| 28767 | AUDUSD | 1 | 4 | sweep AGREE, fvg OPPOSE, fib OPPOSE, ema OPPOSE, vwap OPPOSE |
| 28769 | NZDUSD | 3 | 2 | sweep AGREE, fvg AGREE, fib OPPOSE, ema OPPOSE, vwap AGREE |
| 28771 | EURGBP | 2 | 3 | sweep AGREE, fvg AGREE, fib OPPOSE, ema OPPOSE, vwap OPPOSE |
| 28773 | EURJPY | 2 | 3 | sweep AGREE, fvg AGREE, fib OPPOSE, ema OPPOSE, vwap OPPOSE |
| 28775 | GBPJPY | 3 | 2 | sweep AGREE, fvg AGREE, fib OPPOSE, ema OPPOSE, vwap AGREE |
| 28777 | AUDJPY | 3 | 2 | sweep AGREE, fvg AGREE, fib OPPOSE, ema OPPOSE, vwap AGREE |
| 28779 | CHFJPY | 2 | 3 | sweep AGREE, fvg AGREE, fib OPPOSE, ema OPPOSE, vwap OPPOSE |
| 28781 | XAUUSD | 1 | 4 | sweep AGREE, fvg OPPOSE, fib OPPOSE, ema OPPOSE, vwap OPPOSE |
| 28783 | XAGUSD | 3 | 2 | sweep AGREE, fvg AGREE, fib OPPOSE, ema OPPOSE, vwap AGREE |
| 28785 | XAUEUR | 3 | 2 | sweep AGREE, fvg AGREE, fib OPPOSE, ema OPPOSE, vwap AGREE |
| 28787 | XAUAUD | 3 | 2 | sweep AGREE, fvg AGREE, fib OPPOSE, ema OPPOSE, vwap AGREE |

Scan rows for that cycle are `scan_symbols` id 25217–25232, status `scanned`, `bar_ts=1792389600`. `signals` count stayed 0.

`test_actionable_frame_is_pending_idempotent_and_superseded` is the mocked ScanCycle path: one PENDING, correct `decision_id` and `expires_at`, re-run writes nothing, the next bar supersedes, order calls stay empty. TESTED-ONLY.

## Research Desk and a manual BUY

Cards 01–08 use the status string "Not running yet (planned)" (`dda2687`). Agent 06 still says it needs `FINANCIAL_DATASETS_API_KEY` when that key is unset. The vendor-name guard is in the suite below. The dashboard was not opened. TESTED-ONLY.

A manual BUY on the Exchange tab, from the code, not from a click:

The button label is `BUY · {symbol} · {lots:.2f} lots`. It stays disabled until the order check passes, which includes a stop loss. Clicking it opens the "Confirm order" dialog. Confirm calls `submit_order`. In PAPER that builds `PaperExecutor`. It does not call MT5 `order_send`.

Checks, in order: side is BUY or SELL, volume > 0, config loads, mode is not LIVE (LIVE from the dashboard is refused), broker snapshot is connected, journal account fingerprint matches, symbol spec has point, digits, and tick value/size, a live quote exists, stop loss is present and the price is valid, `RiskEngine.evaluate` returns an approval, the live guard re-reads the anchor at dispatch. Then the paper fill uses a ticket from the 9000000 sequence.

Journal: a `positions` row, a `trades` row with `broker=PAPER` and status OPEN, and an event "paper order filled". The approval's `decision_id` is `"{approved}:{symbol}"`, so the lifecycle events `order_sent` and `order_filled` are written too. The Positions matrix reads that journal row.

Close is a different path. The row's Close button (`close_{ticket}`) opens a confirm dialog, and Confirm close calls `execution.close_position`. That requires a demo account, then `mt5.positions_get` and `mt5.order_send` with magic 100100. A paper ticket is not on the terminal, so the result is `Position #{ticket} not found` and the journal row stays open. `PaperExecutor.close` exists and is not what that button calls.

Click path, if you choose to test it yourself. This session did not click it.

1. Open the dashboard and select the Exchange tab.
2. Select a whitelisted symbol. Set side to BUY. Enter lots greater than 0 and a stop loss greater than 0.
3. Click `BUY · {symbol} · {lots:.2f} lots`.
4. In "Confirm order", click Confirm.
5. On the Positions matrix, click Close on that ticket, then click Confirm close. Expect the not-found result above, and the journal position still open.

## Restart

Read-only counts before the quiet backup, `file:...?mode=ro`: events 28687, heartbeats 158274, signals 0, scan_symbols 25120, trades 0, positions 0.

`backup_journal()` wrote `backups/engine_5057151780_20261019T030742Z.db` (43225088 bytes, integrity check inside the function). An earlier copy from 03:06:55Z exists too. Neither is committed.

The processes still running at 07:40 local were PID 23632 (heartbeat writer) and its parent 26712, both `python -m engine` with no `--live`, parented by the already-dead PID 22208. They were the old code. They were stopped before the backup above. They were not an emergency stop and no close command was sent.

The running engine was started with Win32_Process.Create:

`E:\HedgeFund_Binod_v2\.venv\Scripts\python.exe -u -m engine --config E:\HedgeFund_Binod_v2\config.yaml`

No `--live`. A first start at 03:09Z (interpreter under cmd PID 464) logged `strategy evaluate failed: 'function' object has no attribute 'evaluate'` for all 16 symbols (events 28692–28707) and was stopped. `50bf9a5` calls the function `build_engine` stores. The process below is the one after that fix.

A later command, at 03:17:06Z, found the same interpreter still alive:

| | |
|---|---|
| cmd parent | 34676 |
| venv launcher | 23236, `E:\HedgeFund_Binod_v2\.venv\Scripts\python.exe` |
| interpreter / heartbeat PID | 14352, `C:\Users\Binod\AppData\Local\Python\pythoncore-3.14-64\python.exe` |
| command line | venv python `-u -m engine --config E:\HedgeFund_Binod_v2\config.yaml` |

Startup log (keys omitted): `mode=PAPER`, `db=E:\HedgeFund_Binod_v2\data\engine_5057151780.db`, `magic=100100`, account terminal and journal both MetaQuotes-Demo USD, MATCH.

Startup events: 28714 gateway connected, 28715 Telegram approval layer initialized, 28716 telegram approval layer enabled, 28717 IDLE -> SCANNING. All at 2026-10-19T03:12:35Z. Event 28751 database backed up at 03:12:36Z.

Heartbeats, same PID 14352, connected=1: id 158490 at 03:13:10Z, then id 158724 at 03:17:04Z, 158725 at 03:17:05Z, 158726 at 03:17:06Z. The loop writes a heartbeat on every 1-second command poll. `heartbeat_seconds: 5` does not gate that write.

## Full suite

Command: `.venv\Scripts\python.exe -m pytest -q --tb=line`

```
........................................................................ [ 93%]
........................................................................ [ 97%]
....................................                                     [100%]
============================== warnings summary ===============================
engine\test_signal.py:19
  E:\HedgeFund_Binod_v2\engine\test_signal.py:19: PytestCollectionWarning: cannot collect test class 'TestSignalResult' because it has a __init__ constructor (from: engine/test_signal.py)
    @dataclass(frozen=True)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
1686 passed, 6 skipped, 1 warning in 50.29s
```

Stage 2c was not started. No config key was changed for it. The TEST SIGNAL injector stays off until you say go.
