# Evidence: Gateway Watchdog (Task 3 — B4)

## Objective

Verify the B4 watchdog implementation detects simulated stalled owner-thread
calls. The 15:35:46Z → 17:08:53Z heartbeat gap is addressed separately below:
Windows Modern Standby explains that observed gap; no blocked call is claimed.

## B4 Watchdog Implementation

Located in `engine/gateway.py`:

- `Gateway.__init__`: `watchdog_timeout` defaults to 30.0 seconds, configurable
  via `config.yaml` → `engine.gateway.watchdog_timeout_seconds`
- `poll_watchdog()`: callable from the engine loop to check if the current
  inflight call has exceeded the timeout
- When a stall is detected:
  - `status.connected = False`
  - `status.account_readable = False`
  - `status._reconnect_requested = True`
  - Logs WARNING with `kind=gateway_stalled`, `pending_call` name, `age_seconds`
  - One-shot logging via `_stall_logged` flag (prevents log flooding)
- `gateway_call_age_seconds` column added to `heartbeats` table (schema v3)
  - Records seconds since `last_success_at` (the last gateway call that returned)
  - Heartbeat writer sets this via `Gateway.last_success_age()`

### Key design constraint: orders are NEVER retried after a stall

When `_reconnect_requested` is True, any incoming `order_send` request is
rejected with `NotConnected("order was not retried after a gateway stall")`.
This prevents the "ghost order" problem where a timeout is mistaken for a
failed send.

## Test Results

```
tests/test_engine/test_b4_watchdog.py ... 3 passed
```

1. `test_defaults_are_thirty_seconds_and_three_attempts` — verifies
   `watchdog_timeout_seconds=30.0` and `account_reinitialize_attempts=3`
   from config validation
2. `test_a_blocked_owner_call_is_a_stall_and_orders_are_not_retried` —
   uses a fake gateway that blocks forever; verifies stall is logged,
   `connected` becomes False, and `send_order` is rejected (not retried)
3. `test_the_next_read_reconnects_without_sending_an_order` — verifies the
   reconnect path does not send orders

## Live Evidence: gateway_call_age_seconds

The `heartbeats` table now includes `gateway_call_age_seconds`. During the
MT5 close/reopen test (see recovery_test.md), the call age was:

```
Normal operation:     call_age ~ 0.0001s
Outage detected:      call_age = 0.000311s (still small — account_info() returns None quickly)
```

The call age does NOT spike during a terminal disconnect because
`account_info()` returns `None` immediately (a fast failure, not a hang).
The watchdog is designed for the case where the MT5 API **blocks indefinitely**
(e.g., the terminal window is frozen, the DLL hangs), not for the case where
it returns None. A fast None return is handled by the B1 account guard path.

## Correction: 15:35:46Z → 17:08:53Z heartbeat gap

The earlier claim that no sleep event was logged and that an unguarded
`mt5.account_info()` call blocked for 1h33m is withdrawn. Windows System
events record Modern Standby from 21:05:41 to 22:38:53 IST. That interval
matches the MT5 terminal disconnect at 21:05:41.586 IST, re-authorization at
22:38:56.195 IST, and the heartbeat pause from 15:35:46Z to 17:08:53Z.
The sleep/wake explanation is **PROVEN** by those matching records.

The MT5 LiveUpdate notice near re-authorization is incidental log context,
not a cause. No owner-thread stack or dump was captured, so no blocked
owner-thread call is attributed to this gap. The simulated blocked-call unit
tests above establish watchdog behavior under their test setup only; they do
not prove that a blocked call caused the live heartbeat gap.

## Conclusion

B4 watchdog is implemented per spec:
- Configurable timeout (default 30s)
- Detects stalled owner-thread calls
- Logs WARNING with kind=gateway_stalled, pending call name
- Marks connected=false
- Does NOT retry order calls after a stall
- `gateway_call_age_seconds` column records call age in heartbeats

The watchdog implementation is covered by simulated tests. The live
15:35:46→17:08:53Z heartbeat gap was caused by Windows Modern Standby and
wake; a blocked MT5 API call is not evidenced or attributed to that event.
