# Realised P&L — design only

This document does not change the journal or the learning ledger. `learning/ledger.py` `link_journal` can store `realised_pnl`, and today it inserts NULL because the engine `trades` table has nowhere to read it from.

## What exists

`engine/journal.py` creates `trades` with `id`, `ticket`, `symbol`, `side`, `volume`, `entry_price`, `exit_price`, `sl`, `tp`, `status`, `magic`, `broker`, `ts`, `updated_at`. There is no realised P&L, commission, or swap column.

`learning` `trades_link` has `journal_trade_id`, `prediction_ids`, and `realised_pnl`. `link_journal` copies the trade id and the prediction ids and writes `realised_pnl` as NULL. Calibration therefore cannot score a live outcome from the journal. That is the gap this design closes, later, and only forward.

## Migration

Additive and forward-only. Do not rebuild `data/engine.db` and do not rewrite old rows into a guessed profit.

On journal open, if `trades` exists and has no `realised_pnl_account` column, run:

- `ALTER TABLE trades ADD COLUMN realised_pnl_account REAL`
- `ALTER TABLE trades ADD COLUMN commission REAL`
- `ALTER TABLE trades ADD COLUMN swap REAL`
- `ALTER TABLE trades ADD COLUMN pnl_currency TEXT`

Each `ALTER` is ignored when the column is already there. Existing rows stay NULL. A NULL means "not recorded", not zero. Zero is a real flat result and must be written only when the close calculation ran.

Do not bump a learning `schema_version` for this. The learning table already has `realised_pnl`. The missing data is on the engine side. `link_journal` starts copying `realised_pnl_account` when the column exists, and it still writes NULL when the column is absent or the value is NULL. It does not treat NULL as 0 in summaries.

## How executors record it

Both executors write the three numbers in account currency at the close, not at the open.

`PaperExecutor`, on a full close:

`signed_points = (exit_price - entry_price)` for a buy, and the opposite for a sell.
`gross = signed_points * volume * contract_size`.
`realised_pnl_account = gross - commission - swap`.

`contract_size` comes from the symbol spec the executor already used to size the order. If the spec is missing, the close still happens and `realised_pnl_account` stays NULL. The code must not invent a contract size.

`PaperExecutor`, on a partial close: insert a child close row (or update a `closed_volume` on the same ticket's close event) for the volume that closed. `realised_pnl_account` on that row is the partial formula. The parent ticket stays open for the remainder and its realised column stays NULL until that remainder closes. Do not put the partial result on the still-open parent and also on the child.

`LiveExecutor`: prefer the broker's account-currency profit. MT5 deal history exposes profit, commission, and swap in account currency for the deal. Sum the deals that belong to that close. Store those sums. Do not recompute them from prices when the deal record is present.

Currency conversion: if a deal profit is missing and only a price difference is known, leave `realised_pnl_account` NULL unless the symbol's profit currency is the account currency. Do not multiply by an FX rate the engine guessed. A later backfill may fill the NULL only when a stored conversion rate from that same close exists in the journal.

Commission and swap default to 0 only when the executor ran the close and the broker (or the paper model) reported them as absent. They stay NULL when the close path did not run the calculation.

## Backfill

Forward-only. A one-shot backfill may set `realised_pnl_account` on a historical row only when all of these are true:

- `status` is closed
- `entry_price`, `exit_price`, `volume`, and `side` are present
- the symbol spec's contract size is on record
- the profit currency is the account currency, or a conversion rate stored with that trade is on record
- the column is still NULL

Otherwise the row stays NULL. The backfill does not delete rows, does not change `exit_price`, and does not call the broker. Run it once, write a journal event `realised_pnl backfill` with the count of rows filled and the count left NULL, and do not run it again automatically.

`link_journal` then copies the non-NULL value into `trades_link.realised_pnl`. Rows that are still NULL stay out of any average P&L. They are not zeros.

## Tests

- A new database gains the columns, and a second open does not fail on "duplicate column".
- An old row stays NULL.
- A paper full close with a known contract size matches the formula to the cent.
- A paper close with no contract size leaves NULL and still closes.
- A partial close writes the partial number on the closed volume only.
- A live fake deal's profit, commission, and swap are stored as given, not recomputed.
- A price-only live close in a different currency stays NULL.
- The backfill fills a qualifying row once and does not touch a row that already has a value.
- `link_journal` copies the value and still inserts NULL when the engine column is NULL.
- A summary that averages realised P&L skips NULL.

## Review checklist

- [ ] No historical row is given a profit that was not calculated from stored prices, a stored contract size, and a stored currency match.
- [ ] NULL stays NULL. It is not shown as zero in learning summaries.
- [ ] Partial closes do not double-count.
- [ ] Live numbers come from the broker deal when the deal exists.
- [ ] No guessed FX rate.
- [ ] The migration is additive and safe to run twice.
- [ ] `link_journal` copies the column and does not invent a value.
- [ ] Tests cover the cases above before any backfill is run on `data/engine.db`.
- [ ] The operator has agreed the backfill rules before that command is run.
