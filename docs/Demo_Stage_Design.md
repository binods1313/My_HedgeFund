# Demo Stage Design — Broker DEMO, one symbol, one order at a time

**Status: DESIGN ONLY. No code until approved.** Do not implement this
document. Do not change `config.yaml`. Do not pass `--live`. Do not touch
magic-234000. Brain stays parked. `execution_mode` stays `approve_first`.
Auto-execute stays off.

This stage is the first time the engine may call `mt5.order_send`. PAPER
remains the default. DEMO is a new mode, not an alias of PAPER and not an
alias of LIVE.

---

## 1. Mode name

The engine grows a third mode: **`DEMO`**.

| Mode | Fills | Broker `order_send` | Account `trade_mode` |
|---|---|---|---|
| `PAPER` | Journal-only tickets ≥ 9_000_000 | never | any readable demo is allowed for quotes |
| `DEMO` | Real MetaQuotes-Demo tickets | yes, after `order_check` | must be `ACCOUNT_TRADE_MODE_DEMO` |
| `LIVE` | Real-money tickets | blocked in this stage | refused |

Startup refuses DEMO unless all of these hold:

1. `config.yaml` `mode: DEMO` (new allowed value; current file stays PAPER).
2. CLI has no `--live`.
3. `mt5.account_info().trade_mode == ACCOUNT_TRADE_MODE_DEMO`.
4. Journal fingerprint login/server/currency/magic still match.
5. Magic is `100100`. Magic `234000` is never read, written, closed, or
   adopted.
6. `execution_mode` is `approve_first`.

A REAL or CONTEST account is a hard refuse, same shape as
`execution.require_demo_account()`. A missing `account_info()` is a refuse,
not a pass.

PAPER and DEMO must not share an executor class. A DEMO executor may reuse
gateway helpers; it must not call `PaperExecutor.submit`.

---

## 2. Explicit per-order confirmation

Approve-first is necessary and not sufficient. Every DEMO send also requires
a fresh per-order confirmation.

1. The operator approves the signal (`APPROVE_SIGNAL` with `decision_id`).
2. The engine sizes, builds the request, and runs `order_check`.
3. The UI shows the **checked** request: symbol, side, volume, price, SL, TP,
   filling `FOK`, deviation, magic, comment, `order_check` retcode.
4. The operator confirms that exact request with a one-use token
   (`order_token`, same pattern as the PAPER confirm form). The token binds
   the displayed fields. A changed volume, SL, TP, or symbol invalidates it.
5. Only then may `order_send` run.

A queued `APPROVE_SIGNAL` without a matching unused token does not send.
Telegram may enqueue the approval; it may not skip the token. Duplicate
confirm of a used token is `ALREADY_QUEUED` / already decided.

---

## 3. Minimum lot

First-phase DEMO volume is the broker `volume_min` for EURUSD, expected
**0.01**. Sizing still runs the existing risk formula, then:

1. Floor to `volume_step` with Decimal (same order as `execution._normalize_lot`).
2. If the floored volume is below `volume_min`, **reject**. Do not round up.
3. If the floored volume is above 0.01 in this phase, **reject**. The phase
   cap is 0.01 even when risk would allow more.
4. Clamp never raises the lot.

A missing `volume_min` / `volume_step` is a refuse.

---

## 4. EURUSD-only first phase

The DEMO send path accepts **EURUSD** and nothing else.

- Scan may still evaluate the PAPER whitelist and write `strategy_evaluation`
  events. That is observation, not a send.
- `APPROVE_SIGNAL` for any other symbol in DEMO mode fails closed with
  `demo_symbol_blocked`.
- Manual DEMO order form: EURUSD only. Other symbols are not offered.
- Close is allowed only for engine-magic EURUSD positions this stage opened.

A later phase that adds a second symbol is a new design, not a config tweak.

---

## 5. Mandatory SL and TP

Both stop-loss and take-profit are required on the request.

- Missing SL, missing TP, SL/TP on the wrong side of price, or distance
  inside broker `trade_stops_level` → no `order_check`, no `order_send`.
- SL and TP are prices, not pip counts, at send time.
- PAPER today allows TP optional. DEMO does not inherit that.

A fill that returns without SL or TP is **closed immediately**. It is not
left open and flagged.

Exact flow after `order_send` reports DONE:

1. Read the broker position for that ticket (`positions_get(ticket=…)`).
2. If `sl` and `tp` are both present and on the correct side, journal the
   fill as OPEN and stop here.
3. If `sl` is 0/missing or `tp` is 0/missing: journal the fill with
   `missing_stops=1` and the same `decision_id`. Do not treat it as a
   normal managed OPEN.
4. Build a FOK close request for the full volume (opposite type, current
   bid/ask, magic 100100, deviation ≤ 25).
5. `order_check(close_request)`.
6. Only if the check retcode is `{0, 10009}`: `order_send` the **same**
   close request.
7. If the close is DONE, or timeout-reconcile shows the ticket gone: mark
   `trades` CLOSED, delete `positions`, lifecycle `order_closed` with
   `reason=missing_stops`.
8. No second EURUSD entry is allowed while this ticket is unresolved.

Failure handling if the close itself fails:

| Close outcome | Action |
|---|---|
| `order_check` None or non-DONE | Signal `NEEDS_REVIEW` (`missing_stops_close_check_failed`). Queue `EMERGENCY_STOP`. Halt new entries. Do not retry the close in the submit path. |
| `order_send` rejected (requote, no money, invalid fill, 10027, …) | Same: `NEEDS_REVIEW`, `EMERGENCY_STOP`, no submit-path retry. The kill switch is the only remaining close attempt. |
| `order_send` None / timeout, reconcile finds the ticket **still open** | Outcome `UNKNOWN`. Halt. `NEEDS_REVIEW`. No blind resend. |
| `order_send` None / timeout, reconcile finds the ticket **gone** | Treat as CLOSED (`reason=missing_stops`). |
| Kill-switch close also fails | Stay in `EMERGENCY_STOP`. Operator flattens in the terminal. Engine still must not send a new entry. Magic-234000 stays untouched. |

---

## 6. `order_check` before send

The DEMO submit path is:

```
build request → order_check(request) → interpret check retcode
             → (only on success) order_send(request)
```

Rules:

- `order_check` returning `None` is failure. `last_error` is journaled.
  `order_send` is not called.
- Check retcode not in `{0, TRADE_RETCODE_DONE (10009)}` is failure.
  The broker comment is the `reject_reason`. `order_send` is not called.
- The send request must be byte-for-byte the checked request (same symbol,
  volume, type, price, sl, tp, deviation, magic, filling, comment). A
  rebuilt request after a successful check is a bug.
- `LiveExecutor` / `gateway.send_order` today skip `order_check`. DEMO must
  not reuse that path until the check is inserted. The existing
  `execution.py` check-before-send order is the reference, not the live
  engine gateway.

Tests spy the call order. A mutation that swaps the two calls must fail.

---

## 7. Retcode handling

Every `order_check` and `order_send` retcode is classified. Unknown is
never treated as success.

| Class | Examples | Action |
|---|---|---|
| Done | `10009 TRADE_RETCODE_DONE` | Journal fill, upsert position, same `decision_id` |
| Placed / done-partial | `10008 TRADE_RETCODE_PLACED` if it occurs on a deal | Reconcile; if no ticket, `NEEDS_REVIEW` and halt new entries |
| Rejected, safe | `10004 REQUOTE`, `10006 REJECT`, `10007 CANCEL`, `10013 INVALID_VOLUME`, `10014 INVALID_PRICE`, `10015 INVALID_STOPS`, `10016 INVALID_TRADE_VOLUME`, `10019 NO_MONEY`, `10021 PRICE_CHANGED`, `10022 PRICE_OFF`, `10030 INVALID_FILL` | No retry. Signal `FAILED` with the retcode and comment |
| AutoTrading off | `10027` | Fail with the existing "enable Algo Trading" text. No retry |
| Timeout / None | send returns `None` or raises timeout | Reconcile positions+history for our magic+symbol+comment. If a new ticket exists, adopt it. If none, `NOT_EXECUTED`. If reconcile itself fails, outcome `UNKNOWN`, engine halt, no blind retry |
| Unknown retcode | anything not in the table | `UNKNOWN`, halt, `NEEDS_REVIEW`. Never retry |

No automatic resend. A requote is a new operator confirmation, not a loop.

---

## 8. FOK-only filling

DEMO sets `type_filling = ORDER_FILLING_FOK` on every request.

- If `symbol_info.filling_mode` does not include FOK, refuse before check.
  Do not fall back to IOC or RETURN.
- Partial fills are not accepted. A non-FOK fill in the result is
  `NEEDS_REVIEW` and the kill switch is armed for that ticket.
- Current `execution.py` picks FOK when `filling_mode & 1` else IOC.
  DEMO does not inherit that fallback.

---

## 9. Deviation / slippage

- Request `deviation` is a config field, default **25** points, max **25**
  in this phase. Raising it is an operator-approved config change shown as
  `git diff config.yaml` first.
- `order_check` uses the same deviation as `order_send`.
- After fill, `|fill_price - request_price|` is compared to
  `deviation * point`. Over that band: journal `slippage_exceeded`, do not
  open a second order, leave the fill (it already exists) and flag
  `NEEDS_REVIEW`.
- Drift already used at approval (0.5 × stop distance) still runs before
  check. DEMO does not relax it.

---

## 10. Reconcile broker positions

DEMO reconcile is broker-authoritative for **our magic only**.

On start, after every send, after every close, and on a timed-out send:

1. `positions_get()` with no arguments (never `positions_get(symbol=None)`):
   MetaTrader5 returns `None` instead of a list when either argument is
   passed explicitly, even as `None`, so a no-argument call is the only
   way to receive every open position.
2. Partition: `magic == 100100` vs foreign (including 234000).
3. Foreign: log only. Never close, modify, or adopt.
4. Our-magic broker tickets: upsert journal `positions` and `trades` with
   the broker ticket, not a 9M paper ticket.
5. Journal rows with our magic that the broker no longer shows: mark
   `CLOSED` and delete the position row. Do not revive them on the next
   tick (`f305f51` rule: a CLOSED trade is never hydrated back).
6. PAPER 9M tickets must not appear in DEMO `positions`. A leftover PAPER
   row is deleted from the in-memory book and not sent to the broker.

A mismatch that cannot be classified is `NEEDS_REVIEW` and blocks new
entries.

---

## 11. Journal linkage

One `decision_id` on every row of the chain:

`signals` → `commands` (`APPROVE_SIGNAL` / close) → `order_check` event →
`order_sent` → `order_filled` or `FAILED` → `trades` → `positions` →
`order_closed`.

Required fields:

- `signals.decision_id` unique; DEMO rows have `is_test=0` unless the
  injector was used (injector stays PAPER-only).
- `trades.ticket` is the **broker** ticket. `broker='MT5'`.
- `trades.magic = 100100`.
- Lifecycle events store `decision_id` and `ticket` in `context_json`.
- Comment on the MT5 request includes a short decision-id prefix so
  timeout reconcile can match.

`production_trade_count` already skips `signals.is_test=1`. DEMO fills are
production. TEST SIGNAL remains PAPER-only and must not gain a DEMO send
path.

---

## 12. Kill switch

Existing `EMERGENCY_STOP` is the kill switch. In DEMO it also:

1. Refuses any new `order_check` / `order_send`.
2. Closes **our-magic EURUSD** positions via FOK close, `order_check` first.
3. Leaves magic-234000 and every other magic untouched.
4. Leaves PAPER 9M rows untouched (they are not broker positions).
5. Stays in `EMERGENCY_STOP` until `RESET_EMERGENCY` with the existing
   confirmation phrase.

A second independent stop: if `account.trade_mode` leaves DEMO mid-session,
the engine halts as if `EMERGENCY_STOP` was queued. It does not wait for
the operator.

Position monitoring continues while halted, same as Stage 2.

---

## 13. Blocked items (this stage)

- `--live`, `mode: LIVE`, `LiveExecutor` construction
- `auto_execute`
- Brain / research agents 09–10 placing orders
- Magic 234000 (read-only classify)
- Any symbol other than EURUSD on the send path
- Volume above 0.01
- Missing SL or TP
- IOC / RETURN filling
- Deviation above 25
- Hedging a second EURUSD ticket while one our-magic ticket is open
- Averaging, grid, pending/limit/stop orders
- Telegram skip-confirm
- TEST SIGNAL injector in DEMO mode
- Changing `config.yaml` without showing `git diff` first
- Blind retry after timeout or requote
- Adopting foreign tickets into the journal as ours

---

## 14. Tests (must exist before the first real DEMO send)

All of these are fake-MT5 or journal tests. None of them call a live
terminal until the operator says go.

1. Mode parser accepts `DEMO`, rejects `LIVE` without confirmation, rejects
   `--live` combined with DEMO.
2. `trade_mode != DEMO` refuses before `order_check`.
3. Non-EURUSD approve in DEMO → `demo_symbol_blocked`, zero send calls.
4. Volume 0.00 / below min / above 0.01 → reject, zero send calls.
5. Missing SL, missing TP, wrong-side SL/TP → reject, zero send calls.
   Fill that returns without SL or TP is closed immediately (`order_check`
   first); close-check failure and close-send failure both halt with
   `NEEDS_REVIEW` and never leave a no-stop position as managed OPEN.
6. `order_check` is invoked, and `order_send` is not, on check failure and
   on check `None`.
7. `order_check` precedes `order_send` on the success path (call-order spy).
8. Filling is FOK; a broker without FOK refuses; IOC fallback test fails.
9. Retcode matrix: DONE, REQUOTE, NO_MONEY, INVALID_STOPS, 10027, None,
   unknown → the table in §7.
10. Timeout: filled-on-broker vs never-sent vs reconcile-failed.
11. Per-order token: wrong token, reused token, mutated volume after check.
12. Reconcile: our-magic upsert; 234000 log-only; CLOSED not revived
    (`f305f51` equivalent on broker tickets).
13. Kill switch: no new send; our EURUSD flattened; 234000 untouched.
14. Journal: same `decision_id` on signal, command, events, trade, position.
15. `is_test=1` still excluded from `production_trade_count`.
16. Two sequential EURUSD sends while one is open → second refused.

Mutation checks (same style as `scripts/mutation_check.py`) for
check-before-send, FOK-only, and DEMO account guard.

---

## 15. Exit criteria

DEMO stage is **PROVEN** only when all of the following are true on the
MetaQuotes-Demo account, with raw journal rows pasted.

**Any failed leg stops the stage until reviewed.** Do not continue to the
next send, close, or restart until that leg is classified and the operator
says go.

1. Account `trade_mode` DEMO, login matches journal fingerprint, magic
   100100, no `--live`.
2. Record DEMO account **balance (and equity) immediately before the first
   send** and **after the last close**. Paste both numbers in the report
   next to the tickets. A missing before/after pair fails the stage.
3. One EURUSD 0.01 BUY with SL and TP: `order_check` DONE, `order_send`
   DONE, broker ticket in `trades`, same `decision_id` through close.
4. One EURUSD 0.01 SELL round-trip, same rules.
5. One rejected path: `order_check` failure never calls `order_send`.
6. One FOK-unsupported or invalid-fill refuse (if the broker can show it;
   otherwise a fake-MT5 test plus a journal note that live FOK was
   accepted on EURUSD).
7. Kill switch: EMERGENCY_STOP while a DEMO ticket is open closes that
   ticket and leaves 234000 untouched.
8. Reconcile after restart: journal positions == our-magic broker
   positions; zero OPEN paper 9M rows; zero ghost CLOSED tickets.
9. `production_trade_count` includes the DEMO tickets and still excludes
   historical TEST SIGNAL 9000002.
10. No `config.yaml` change beyond the approved `mode: DEMO` switch, shown
    as `git diff` before restart.
11. Full unit suite green.

Until those hold, the stage is TESTED-ONLY or UNPROVEN. A single
happy-path fill is not exit.

---

## 16. What this document does not do

It does not schedule implementation. It does not authorize a DEMO restart.
It does not authorize a BUY. PAPER ticket 9000001 / 9000002 stay CLOSED
history. The running PAPER engine is left alone until the operator
approves this design and a verified backup-then-restart procedure.
