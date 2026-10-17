# Engine soak report

Documentation only. Written 2026-10-16 from read-only extracts taken on
2026-10-15. The live journal was not opened again on 2026-10-16, and it
was not opened through `engine.journal.Journal`. PID 44436 was not
restarted. This file covers the soak, the intentional offline test, and
what the same process did afterward. It does not cover Stage 1.

## Window

| Item | Value |
|---|---|
| Process start | 15-10-2026 18:53:46 local. Journal clock `2026-10-15T13:23:46+00:00`. Local offset versus the journal UTC strings is +05:30. |
| Clean soak start | `2026-10-15T13:23:46+00:00` inclusive |
| Clean soak end | Last equity row `2026-10-15T14:16:54+00:00`. Offline event `2026-10-15T14:16:55+00:00`. About 53 minutes. |
| Process end | None. The same PID was still the 18:53:46 process when this package was written. |
| PID | 44436. Parent 20288. Command `python -u -m engine`. No `--live`. |
| Mode | PAPER. Magic `100100`. |
| Configured heartbeat | `heartbeat_seconds` 5, `offline_after_seconds` 15, `command_poll_seconds` 1. |
| Observed heartbeat rate in the clean window | About 1 Hz. `run_once` writes a heartbeat on every completed iteration, and the poll sleep is 1 second. The 5-second setting is not the write rate that was observed. |

A 30-minute watcher inside the clean window exited 0. Its only line:

```
DONE: soak 1800s pid 44436 last 2026-10-15T13:55:39+00:00 hb_id=53787 hb_ts=2026-10-15T13:55:38+00:00 pid=44436 connected=1 state=IDLE bad=0
```

That line is before the intentional close. It is not a later failure.

## Heartbeat totals

Clean window, `ts` from `2026-10-15T13:23:46+00:00` inclusive to
`2026-10-15T14:16:55+00:00` exclusive.

Aggregate `(count, connected=0 count, other-pid count)`:

```
HB_COUNT (3056, 0, 0)
```

3056 heartbeats. Zero with `connected=0`. Zero from any PID other than
44436. State in the sampled rows is `IDLE`.

Representative rows. Not the full series.

First five, one per second:

```
51877  2026-10-15T13:23:46+00:00  IDLE  connected=1  pid=44436
51878  2026-10-15T13:23:47+00:00  IDLE  connected=1  pid=44436
51879  2026-10-15T13:23:48+00:00  IDLE  connected=1  pid=44436
51880  2026-10-15T13:23:49+00:00  IDLE  connected=1  pid=44436
51881  2026-10-15T13:23:50+00:00  IDLE  connected=1  pid=44436
```

Middle of the clean window:

```
52730  2026-10-15T13:38:00+00:00  IDLE  connected=1  pid=44436
52731  2026-10-15T13:38:01+00:00  IDLE  connected=1  pid=44436
52732  2026-10-15T13:38:02+00:00  IDLE  connected=1  pid=44436
52733  2026-10-15T13:38:03+00:00  IDLE  connected=1  pid=44436
52734  2026-10-15T13:38:04+00:00  IDLE  connected=1  pid=44436
```

Last five before the offline event. Id 54932 is the newest:

```
54928  2026-10-15T14:16:50+00:00  IDLE  connected=1  pid=44436
54929  2026-10-15T14:16:51+00:00  IDLE  connected=1  pid=44436
54930  2026-10-15T14:16:52+00:00  IDLE  connected=1  pid=44436
54931  2026-10-15T14:16:53+00:00  IDLE  connected=1  pid=44436
54932  2026-10-15T14:16:54+00:00  IDLE  connected=1  pid=44436
```

`connected=1` is the gateway flag stored with the heartbeat. It is not
proof that the terminal answered that iteration.

## Equity totals

Same clean window.

Aggregate `(count, first id, last id, first ts, last ts)`:

```
EQ_COUNT (3056, 28291, 31346, 2026-10-15T13:23:46+00:00, 2026-10-15T14:16:54+00:00)
```

Every sampled equity row in that window:

```
equity = 99984.47
balance = 99984.47
margin = 0
open_risk = 0
open_positions = 0
```

First three and the last row:

```
28291  2026-10-15T13:23:46+00:00  equity=99984.47  balance=99984.47  margin=0  open_risk=0  open_positions=0
28292  2026-10-15T13:23:47+00:00  equity=99984.47  balance=99984.47  margin=0  open_risk=0  open_positions=0
28293  2026-10-15T13:23:48+00:00  equity=99984.47  balance=99984.47  margin=0  open_risk=0  open_positions=0
31346  2026-10-15T14:16:54+00:00  equity=99984.47  balance=99984.47  margin=0  open_risk=0  open_positions=0
```

After the offline event, the maximum equity id stayed 31346 at
`2026-10-15T14:16:54+00:00` with equity `99984.47`. Telemetry writes stop
when the account guard returns false from `may_write_telemetry()`. That
is why equity stopped while heartbeats continued.

## Events inside the clean window

Startup rows for this process, then the backup row. Ids 23623–23625 are
the earlier `--check-config` process, not PID 44436.

```
23626  2026-10-15T13:23:46+00:00  gateway connected
23627  2026-10-15T13:23:46+00:00  telegram initialized
23628  2026-10-15T13:23:46+00:00  telegram approval layer enabled
23629  2026-10-15T13:23:46+00:00  backup database backed up
```

The chat id on the telegram rows is omitted.

`BAD_SOAK` for event ids `>= 23626` and `< 23630`:

```
(0,)
```

No events sit between 23629 and 23630. Commands with
`ts >= 2026-10-15T13:23:46+00:00`: count 0.

`engine_state` at startup:

```
id 1  IDLE  PAPER  "account fingerprint verified at startup"
updated_at 2026-10-15T13:23:46+00:00
login 5057151780  USD
```

`journal_account` was recorded earlier and was not rewritten by this soak:

```
recorded_at 2026-10-13T19:51:15+00:00
magic 100100
```

## Offline test

The terminal was closed on purpose at local 2026-10-15 19:46:53
(`2026-10-15T14:16:53+00:00`). The engine wrote this row. The events
column is `context_json`.

```
23630  2026-10-15T14:16:55+00:00  WARNING  Terminal offline
{"phase": "session_refresh", "reason": "no account is logged in; broker or terminal not answering", "kind": "terminal_offline", "repeat": false, "configured_login": 5057151780, "configured_currency": null, "live_login": null, "live_currency": null, "live_server": null, "journal_login": 5057151780, "blocked": ["trading", "reconcile", "telemetry", "order_submission", "order_modification"]}
```

Severity is `WARNING`. Kind is `terminal_offline`. This is the unresolved
terminal path, not the `CRITICAL` / `account_mismatch` path.

Reminder rows. Same context as 23630, with `"repeat": true`, and the
message `Terminal offline (still unresolved)`:

```
23631  2026-10-15T14:26:55+00:00
23632  2026-10-15T14:36:56+00:00
23633  2026-10-15T14:46:56+00:00
23634  2026-10-15T14:56:57+00:00
23635  2026-10-15T15:20:52+00:00
23636  2026-10-15T15:30:53+00:00
23637  2026-10-15T17:08:52+00:00
```

The first four reminders are about 10 minutes apart. That matches
`_REMIND_EVERY = 600` seconds on the account-guard path in the code this
process is running. The gaps after 14:56:57Z are longer. See anomalies.

Heartbeats did not switch to `connected=0` during or after this test.

## Recovery result

There is no recovery row.

A later independent probe called `mt5.initialize()` and read the account
while event 23637 was still the newest engine event and was still
`Terminal offline (still unresolved)`.

Paired read:

```
HB 57799  2026-10-15T17:15:01+00:00  IDLE  connected=1  pid=44436
EQ max still 31346 at 2026-10-15T14:16:54+00:00 equity 99984.47
event 23637 still unresolved
PROBE_INIT True (1, 'Success')
PROBE_LOGIN 5057151780 MetaQuotes-Demo trade_mode 0 equity 99984.47
PROBE_POS 0
```

`check()` success clears the guard's active reason and does not write a
recovery row. `_ensure_connected` returns immediately when
`status.connected` is already true. `account_info()` returning `None` is
a successful call from the gateway's point of view, so the stored
`connected` flag can stay 1.

Automatic recovery of PID 44436 is **BLOCKED**. A reconnect change was
not applied.

## Observed anomalies

1. **PROVEN.** Equity stopped at id 31346 while heartbeats continued with
   `connected=1`. The guard blocked telemetry. The gateway flag did not
   follow the terminal outage.
2. **PROVEN.** No recovery event was written after the terminal was
   logged in again. The newest engine event stayed 23637.
3. **PROVEN (sleep/wake cause).** Consecutive heartbeat ids for this PID
   jump across the Windows Modern Standby interval:

```
LAST_BEFORE  57478  2026-10-15T15:35:46+00:00  connected=1  pid=44436
FIRST_AFTER  57479  2026-10-15T17:08:53+00:00  connected=1  pid=44436
             57480  2026-10-15T17:08:53+00:00  connected=1  pid=44436
             57481  2026-10-15T17:08:54+00:00  connected=1  pid=44436
```

   In `ts >= 2026-10-15T15:30:53+00:00 AND ts < 2026-10-15T17:08:52+00:00`
   there were 226 heartbeats, and the maximum `ts` in that set is
   `2026-10-15T15:35:46+00:00`. The gap from 57478 to 57479 is about 93
   minutes. Windows System events record Modern Standby from 21:05:41 to
   22:38:53 IST; the MT5 log records loss and re-authorization at the same
   boundaries. This proves the sleep/wake explanation for the gap. No claim
   is made that an MT5 owner-thread call was blocked.
4. **UNPROVEN.** Reminder spacing left the 10-minute cadence after
   14:56:57Z (next rows 15:20:52Z and 17:08:52Z). The cause was not
   established. The 17:08:52Z reminder is the same second-band as the
   heartbeat resuming at 17:08:53Z. That pairing was observed. It was not
   explained.

SQL filters used explicit ISO strings. `datetime('now')` does not match
these timestamps.

## Conclusions

| Claim | Label |
|---|---|
| PID 44436 ran PAPER from 13:23:46Z and wrote 3056 heartbeats and 3056 equity rows through 14:16:54Z, all `connected=1`, equity 99984.47, no open positions. | PROVEN |
| The intentional close produced event 23630, kind `terminal_offline`, and reminders 23631–23637. | PROVEN |
| Equity writes stopped at 31346 because telemetry is blocked while the guard is unresolved. | PROVEN |
| The process recovered by itself and wrote a recovery row. | Not observed. BLOCKED. |
| Heartbeat `connected=0` records the outage. | Not observed. The flag stayed 1. |
| Cause of the 15:35:46Z–17:08:53Z heartbeat gap: Windows Modern Standby sleep/wake. | PROVEN |
| Cause of the late reminders after 14:56:57Z. | UNPROVEN |
| This soak exercised Stage 1. | No. The process started before `engine/scan_cycle.py` existed. |
