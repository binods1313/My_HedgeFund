# Symbol Search — live terminal, 2026-10-15

Read-only probe of the terminal that is logged into account 5057151780
on server MetaQuotes-Demo. Calls used: `symbols_get()`, `symbol_info()`,
`symbol_info_tick()`. No order was sent. `config.yaml` was not edited.

The account at probe time: login 5057151780, currency USD, balance
99984.47, equity 99984.47, `positions_get()` count 0.
`symbols_get()` returned 12299 symbols. `account_info().trade_mode`
was 0, which on this MetaTrader5 build is the demo account mode.

`config.yaml` still says the metals list is empty because
XAUUSD/XAGUSD/XTIUSD/XBRUSD are not offered. That sentence is false
for this terminal. XAUUSD is present and its trade mode is FULL.
The yaml file was left unchanged.

This package has no `symbol_info_session_*` functions
(`SESSION_APIS` from `dir(mt5)` was empty). Session fields below are
the attributes on the `symbol_info` object, not a session calendar.

Trade mode 0 is `SYMBOL_TRADE_MODE_DISABLED`. Trade mode 4 is
`SYMBOL_TRADE_MODE_FULL`. Stops level is `trade_stops_level`.

## Candidates

| Symbol | Description | Path | Contract | Tick | Volume step | Stops | Trade mode |
|---|---|---|---|---|---|---|---|
| BTCUSD | — | — | — | — | — | — | MISSING |
| BTCUSD# | — | — | — | — | — | — | MISSING |
| ETHUSD | — | — | — | — | — | — | MISSING |
| ETHUSD# | — | — | — | — | — | — | MISSING |
| US500 | US SPX 500 Index | Indexes\US500 | 1.0 | 0.01 | 0.1 | 0 | 0 DISABLED |
| US500M | S&P 500mini | Indexes\US500M | 10.0 | 0.1 | 1.0 | 100 | 0 DISABLED |
| US500Cash | — | — | — | — | — | — | MISSING |
| US500Cash# | — | — | — | — | — | — | MISSING |
| SPX500 | — | — | — | — | — | — | MISSING |
| US30 | US Wall Street 30 Index | Indexes\US30 | 1.0 | 0.01 | 0.1 | 0 | 0 DISABLED |
| US30M | US30m | Indexes\US30M | 0.1 | 1.0 | 1.0 | 5 | 0 DISABLED |
| US30Cash | — | — | — | — | — | — | MISSING |
| US30Cash# | — | — | — | — | — | — | MISSING |
| US100 | — | — | — | — | — | — | MISSING |
| US100Cash | — | — | — | — | — | — | MISSING |
| US100Cash# | — | — | — | — | — | — | MISSING |
| NAS100 | — | — | — | — | — | — | MISSING |
| USTEC | US Tech 100 Index | Indexes\USTEC | 1.0 | 0.01 | 0.1 | 0 | 0 DISABLED |
| USTECH100M | USTECH100mini | Indexes\USTECH100M | 1.0 | 0.1 | 1.0 | 100 | 0 DISABLED |
| UK100 | FTSE 100 Index | Indexes\UK100 | 10.0 | 0.1 | 0.1 | 0 | 0 DISABLED |
| UK100Cash | — | — | — | — | — | — | MISSING |
| UK100Cash# | — | — | — | — | — | — | MISSING |
| FTSE100 | — | — | — | — | — | — | MISSING |
| XAUUSD | Gold vs US Dollar | Metals\XAUUSD | 100.0 | 0.01 | 0.01 | 0 | 4 FULL |
| XAUUSD# | — | — | — | — | — | — | MISSING |

Prefix counts from `symbols_get()`: BTCUSD 0, ETHUSD 0, XAUUSD 1
(`XAUUSD`), US500 2 (`US500`, `US500M`), US30 2 (`US30`, `US30M`),
US100 0, NAS100 0, FTSE 0, UK100 1 (`UK100`).

Also present, not requested as the primary candidate: XAUEUR and
XAUAUD are FULL (contract 100, tick 0.01, volume step 0.01, stops 0).
XAUCHF and XAUGBP are DISABLED (contract 100000, tick 0.01, volume
step 1e-08, stops 5). DE40 is DISABLED (contract 1, tick 0.01,
volume step 0.1, stops 0).

## XAUUSD tick and session

```
XAU_SESSION open 4106.46 close 4101.4 aw 0.0 deals 0 volume 0.0 turnover 0.0 exemode 2 start 0 expiration 0 spread 32
XAU_TICK time 1792089266 msc 1792089266641 bid 4156.15 ask 4156.47 spread_price 0.32
```

`spread` 32 is the symbol-info field. `spread_price` 0.32 is ask minus
bid on that tick. `start_time` and `expiration_time` are 0. Session
deals, volume, and turnover are 0. No session-schedule API was available
to query.
