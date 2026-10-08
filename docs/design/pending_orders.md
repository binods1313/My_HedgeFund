# Pending orders — design only

This document does not change the engine. Limit, stop, and conditional orders stay unavailable in the UI until an operator signs the checklist at the end. The current caption remains `engine does not support this yet`. Market orders, Submit, and Close are unchanged.

## States

An order row has one of these states:

| State | Meaning |
| --- | --- |
| PENDING | Accepted onto the book. It has not traded. |
| TRIGGERED | The price condition became true. Risk runs again before any fill. |
| FILLED | The broker or the paper book reports a position. |
| CANCELLED | The operator cancelled it, or a restart rule cancelled it. |
| EXPIRED | `expires_at` passed while it was still PENDING. |
| REJECTED | Placement or the trigger-time risk check refused it. The row keeps the reason. |

There is no path from FILLED back to PENDING. CANCELLED, EXPIRED, and REJECTED are terminal. TRIGGERED moves only to FILLED or REJECTED.

## Where state lives

State lives in a new `pending_orders` table in the engine journal (`data/engine.db`), written by the engine process. The UI inserts a command row and then only reads. Session state must not be the book: a browser refresh is not a cancel, and it is not a fill.

Suggested columns: `id`, `client_id` (unique), `symbol`, `side`, `kind` (`limit`, `stop`, `conditional`), `volume`, `trigger_price`, `sl`, `tp`, `expires_at`, `state`, `reason`, `ticket`, `created_at`, `updated_at`. `client_id` is the idempotency key.

## Paper fill and live quotes

Paper and live use the same trigger rule. The engine evaluates the latest quote it already stores. It does not invent a price.

- A buy limit triggers when ask is at or below `trigger_price`. A sell limit triggers when bid is at or above it.
- A buy stop triggers when ask is at or above `trigger_price`. A sell stop triggers when bid is at or below it.
- A conditional order is a limit or a stop plus a second predicate that is also data the engine already has (for example "only if spread is at most N points"). The predicate is stored as fields, not as code.

Gaps: if the quote jumps through the trigger, the fill price is the quote that crossed, not the trigger price. A buy stop whose trigger is 1.1000 and whose first ask after the gap is 1.1040 fills at 1.1040 in paper, and at the broker's fill in live. The journal records both the trigger and the fill. A missing quote does not trigger. A stale quote (the same age rule the header already uses) does not trigger.

Live quotes come from the gateway's existing quote read. The UI quote is not the trigger input.

## Risk at placement and again at trigger

`engine/risk.py` runs twice.

1. At placement, the order is rejected unless it has a stop, the size is inside the current caps, and adding the worst-case loss would still pass the open-risk cap. A pending order counts toward that cap while it is PENDING or TRIGGERED, because it can become a position without another click.
2. At trigger, the same check runs on the fresh account and the fresh quote. If it fails, the state is REJECTED and nothing is sent. The placement check is not reused as a token that stays valid.

The stop is required at placement. An order with no stop is REJECTED and is not stored as PENDING. Take-profit may be empty. The UI confirm dialog's `tp_units = stop_units * 2` rule is unchanged for market orders and is the default suggestion for a pending order, not a silent fill.

## Expiry and the closed market

`expires_at` is required. The default in paper is the next session end the operator picks, not "good till cancelled" on day one. When `expires_at` passes, the engine sets EXPIRED and writes one journal event. It does not send a market order instead.

When the symbol's market is closed, placement may still store PENDING if the operator chose an expiry after the reopen. The trigger does not fire on a closed market and does not fire on the last quote from the previous session. The first quote after the reopen is evaluated once. A gap across the closed session uses the gap rule above.

## Journal events

Each transition writes one `events` row, source `pending_orders`, with the `client_id`, the old state, the new state, and the reason. Suggested messages: `pending placed`, `pending triggered`, `pending filled`, `pending cancelled`, `pending expired`, `pending rejected`. These are audit rows. They are not a second copy of the state.

## Idempotency and restart

The UI sends one command, `PLACE_PENDING` or `CANCEL_PENDING`, through the existing `commands` table and `Journal.enqueue_command`. `dedupe_key` is the `client_id`. A repeated command for the same `client_id` does not create a second row.

`CommandProcessor` already claims a command with a conditional update, so a restart does not run a command that was in flight. Pending orders are different: a PENDING row is still PENDING after a restart. The engine reloads PENDING and TRIGGERED rows on startup and does not mark them FILLED just because the process died. A TRIGGERED row that had not received a broker result is reconciled the same way `Gateway.send_order` already reconciles a timeout (history and positions), and only then becomes FILLED or REJECTED. It is not sent a second time while that reconciliation is unknown.

## LIVE mapping through the gateway

`engine/gateway.py` `send_order` today submits the request it is given and interprets `TRADE_RETCODE_DONE`. The close path builds `TRADE_ACTION_DEAL`. Pending orders need a separate gateway method, not a reuse of the market deal request.

| Kind | MT5 request |
| --- | --- |
| Buy limit | `TRADE_ACTION_PENDING`, `ORDER_TYPE_BUY_LIMIT` |
| Sell limit | `TRADE_ACTION_PENDING`, `ORDER_TYPE_SELL_LIMIT` |
| Buy stop | `TRADE_ACTION_PENDING`, `ORDER_TYPE_BUY_STOP` |
| Sell stop | `TRADE_ACTION_PENDING`, `ORDER_TYPE_SELL_STOP` |

`price` is the trigger. `sl` and `tp` are absolute prices. `type_time` is `ORDER_TIME_SPECIFIED` with `expiration` set from `expires_at`. Magic stays the engine magic. The gateway still refuses to send when it is disconnected, and a timeout is reconciled rather than retried. Cancel uses `TRADE_ACTION_REMOVE` with the pending ticket. This mapping is implemented only inside `engine/gateway.py`. The UI must not import MetaTrader5.

Paper mode does not call that method. `PaperExecutor` applies the trigger rule locally and records a paper position. `LiveExecutor` calls the gateway method only when mode is LIVE and the existing live confirmation is set.

## Files that would change later

- `engine/journal.py` — table and the event writes
- `engine/gateway.py` — pending send and cancel
- `engine/executor.py` — paper trigger and live call
- `engine/risk.py` — placement check and trigger check
- `engine/commands.py` — `PLACE_PENDING` and `CANCEL_PENDING` handlers
- `tests/test_engine/fake_mt5.py` — pending retcodes, a gap quote, and a timeout

`engine/state.py` does not need a new machine state. A pending order is a row, not a state-machine mode. No change to Submit or Close for market orders.

## Tests to write with the implementation

Use `tests/test_engine/fake_mt5.py`. Do not use a live terminal.

- Placement without a stop is REJECTED and sends nothing.
- The same `client_id` twice yields one row.
- A quote that gaps through a buy stop fills at the gap price in paper and records both prices.
- No quote, a stale quote, and a closed market do not trigger.
- Risk failure at trigger moves TRIGGERED to REJECTED and does not call `order_send`.
- Expiry moves PENDING to EXPIRED.
- A restart leaves PENDING pending, and an in-flight command is not claimed twice.
- LIVE request fields match the table above. PAPER never builds a `TRADE_ACTION_PENDING` request.

## Rollout

1. Ship the table, the paper trigger, and the tests. The UI control stays disabled.
2. Enable the paper control for one symbol after the tests pass.
3. LIVE stays off until the operator signs the checklist below in writing. A config flag defaulting to off is not a signature.

## Review checklist

- [ ] State is only in the journal table, not in `st.session_state`.
- [ ] Every pending order has a stop, an expiry, and a `client_id`.
- [ ] Risk runs at placement and again at trigger, and the trigger check can still reject.
- [ ] A gap fills at the crossed quote, and the journal stores both prices.
- [ ] A missing, stale, or closed-market quote does not trigger.
- [ ] Timeout and restart reconcile. They do not send a second order.
- [ ] LIVE uses `TRADE_ACTION_PENDING` only inside `engine/gateway.py`.
- [ ] Paper never calls the gateway pending method.
- [ ] `tests/test_engine/fake_mt5.py` covers the cases above.
- [ ] The operator has signed before any LIVE pending order is enabled.
- [ ] Market Submit and Close behaviour are unchanged.
