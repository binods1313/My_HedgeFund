# Live symbol survey — account 5057151780

Written 2026-10-16 from the 2026-10-15 read-only probe. The probe called
`mt5.shutdown()` at the end, which disconnects the probe only. PID 44436
was not restarted. `config.yaml` was not edited.

This document records what the terminal offered. It does not recommend a
configuration change.

## Account

```
PROBE_INIT True ERR (1, 'Success')
PROBE_LOGIN 5057151780
SERVER MetaQuotes-Demo
CURRENCY USD
TRADE_MODE 0
BALANCE 99984.47
EQUITY 99984.47
POSITIONS 0
SYMBOLS_GET_COUNT 12299
```

`trade_mode` 0 on this MetaTrader5 build is the demo account mode. It
is not "trading disabled" for the account. Symbol trade mode is a
different field and is reported per symbol below.

Login digits in the captured `--check-config` block were masked as
`****1780`. The probe login number matched the journal login.

## Server

`MetaQuotes-Demo`.

## Methodology

One independent `mt5.initialize()` while the terminal was logged in.
`symbols_get()` for the count. `symbol_info()` for each candidate name.
Prefix groups from the names the terminal returned.

Fields recorded when a symbol existed:

- description and path
- `trade_contract_size`
- tick size
- `volume_step`
- `trade_stops_level` (this build uses that attribute; `stops_level` was
  not the field that returned a value)
- `trade_mode`

Trade mode values used in this survey:

| Value | Meaning in this report |
|---|---|
| 0 | `SYMBOL_TRADE_MODE_DISABLED` |
| 4 | `SYMBOL_TRADE_MODE_FULL` |

`market_open` in the Stage 1 gateway snapshot is `trade_mode == 4`. That
code was not running inside PID 44436. It is mentioned only so the FULL
versus DISABLED results have a reader in the source tree.

Session functions `symbol_info_session_*` are absent on this build.
`SESSION_APIS` was an empty list. No session open/close clock was
derived from those APIs.

For XAUUSD a tick was read. The tick `time` field is pasted as the raw
integer the terminal returned. This note does not convert it and does
not name a timezone for it.

Names checked and absent are listed as missing. They are not inferred
from a comment in `config.yaml`.

## Gold

**XAUUSD exists and is tradable.** Trade mode FULL.

```
XAUUSD | Gold vs US Dollar | Metals\XAUUSD
contract 100.0 | tick 0.01 | vol_step 0.01 | trade_stops_level 0 | trade_mode FULL (4)
symbol spread field 32
session quote fields on the symbol: open 4106.46  close 4101.4
deals 0  volume 0  turnover 0  exemode 2  start 0  expiration 0
```

Tick captured with the probe. `time` and `time_msc` are raw terminal
integers:

```
time 1792089266
time_msc 1792089266641
bid 4156.15
ask 4156.47
spread_price 0.32
```

Prefix `XAUUSD`: `['XAUUSD']`, count 1.

`XAUUSD#` is missing.

Other gold names seen in the same probe:

```
XAUEUR  FULL   contract 100  tick 0.01  vol_step 0.01  stops 0
XAUAUD  FULL   contract 100  tick 0.01  vol_step 0.01  stops 0
XAUCHF  DISABLED  contract 100000  tick 0.01  vol_step 1e-08  stops 5
XAUGBP  DISABLED  contract 100000  tick 0.01  vol_step 1e-08  stops 5
```

`config.yaml` still has a metals comment that says XAUUSD, XAGUSD,
XTIUSD, and XBRUSD are not offered. That comment is false for XAUUSD on
this terminal. The file was left as it was.

## Indices

Present and `SYMBOL_TRADE_MODE_DISABLED`:

```
US500       | US SPX 500 Index       | Indexes\US500        | contract 1.0  | tick 0.01 | vol_step 0.1 | stops 0   | DISABLED
US500M      | S&P 500mini            | Indexes\US500M       | contract 10.0 | tick 0.1  | vol_step 1.0 | stops 100 | DISABLED
US30        | US Wall Street 30 Index| Indexes\US30         | contract 1.0  | tick 0.01 | vol_step 0.1 | stops 0   | DISABLED
US30M       | US30m                  | Indexes\US30M        | contract 0.1  | tick 1.0  | vol_step 1.0 | stops 5   | DISABLED
USTEC       | US Tech 100 Index      | Indexes\USTEC        | contract 1.0  | tick 0.01 | vol_step 0.1 | stops 0   | DISABLED
USTECH100M  | USTECH100mini          | Indexes\USTECH100M   | contract 1.0  | tick 0.1  | vol_step 1.0 | stops 100 | DISABLED
UK100       | FTSE 100 Index         | Indexes\UK100        | contract 10.0 | tick 0.1  | vol_step 0.1 | stops 0   | DISABLED
DE40        | (present in the probe) |                      | DISABLED
```

Prefix counts:

```
US500  ['US500', 'US500M']  n 2
US30   ['US30M', 'US30']    n 2
UK100  ['UK100']            n 1
US100  n 0
NAS100 n 0
FTSE   n 0
```

Missing index names from this probe:

```
US500Cash   US500Cash#   SPX500
US30Cash    US30Cash#
US100       US100Cash    US100Cash#   NAS100
UK100Cash   UK100Cash#   FTSE100
```

The whitelist names that end in `#` are not on this MetaQuotes-Demo
terminal. DISABLED means the symbol object exists and
`trade_mode` is 0. It does not mean the name is absent.

## Crypto

Missing. Prefix counts are 0.

```
BTCUSD    absent
BTCUSD#   absent
ETHUSD    absent
ETHUSD#   absent
PREFIX BTCUSD  0
PREFIX ETHUSD  0
```

No BTC or ETH symbol was returned by those prefixes on this server
during this probe. The survey did not search every crypto description
in the 12299-name list beyond those prefixes and exact names.

## What this survey does not say

- It does not say a symbol should be added to or removed from
  `config.yaml`.
- It does not interpret tick `time` 1792089266 as a civil clock.
- It does not claim session hours. The session APIs were not present.
- It does not claim PID 44436 scanned any of these names. The running
  process has no Stage 1 cycle.
