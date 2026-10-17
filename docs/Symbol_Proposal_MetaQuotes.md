# Live Symbol Proposal — MetaQuotes-Demo

**Status: APPROVED and applied in `config.yaml`.** The scan interval remains
60 seconds. No Stage 2 or order execution was started.

## Probe

Read-only `MetaTrader5.symbol_info()` and `symbol_info_tick()` calls against the
already-running terminal. Probe UTC: `2026-10-16T15:10:31.863613+00:00`.
`symbols_get()` returned 12,295 symbols. Trade mode `4` is FULL; `0` is
DISABLED. Tick times below are raw integers returned by MT5, not converted.

Exact command:

```powershell
@'
import MetaTrader5 as mt5
from datetime import datetime, timezone
names = [
'EURUSD','GBPUSD','USDJPY','USDCAD','USDCHF','AUDUSD','NZDUSD',
'EURGBP#','EURJPY#','GBPJPY#','BTCUSD#','ETHUSD#','US500Cash#','US30Cash#','US100Cash#','UK100Cash#',
'EURGBP','EURJPY','GBPJPY','BTCUSD','ETHUSD','US500Cash','US30Cash','US100Cash','UK100Cash',
'XAUUSD','XAUEUR','XAUAUD','XAGUSD','XAUUSD#','XAGUSD#','XTIUSD','XBRUSD',
'AUDJPY','CHFJPY','AUDNZD','GBPCHF','GBPCAD','EURAUD','EURCHF','NZDJPY','GBPAUD','AUDCAD','CADJPY','NVDA',
'USTEC','US500','US30','UK100','DE40','US500M','US30M','USTECH100M'
]
if not mt5.initialize(timeout=10000):
 print('INITIALIZE_FAILED',mt5.last_error()); raise SystemExit(2)
print('PROBE_UTC',datetime.now(timezone.utc).isoformat())
print('SYMBOLS_GET_COUNT',len(mt5.symbols_get() or ()))
print('name|exists|trade_mode|contract_size|tick_size|volume_step|stops_level|spread|tick_time')
for name in names:
 info=mt5.symbol_info(name)
 if info is None:
  print(f'{name}|False|None|None|None|None|None|None|None'); continue
 tick=mt5.symbol_info_tick(name)
 print('|'.join(str(v) for v in (name,True,getattr(info,'trade_mode',None),getattr(info,'trade_contract_size',None),getattr(info,'trade_tick_size',None),getattr(info,'volume_step',None),getattr(info,'trade_stops_level',None),getattr(info,'spread',None),getattr(tick,'time',None) if tick else None)))
mt5.shutdown()
'@ | .\.venv\Scripts\python.exe -
```

Raw output:

```text
PROBE_UTC 2026-10-16T15:10:31.863613+00:00
SYMBOLS_GET_COUNT 12295
name|exists|trade_mode|contract_size|tick_size|volume_step|stops_level|spread|tick_time
EURUSD|True|4|100000.0|1e-05|0.01|0|1|1792174233
GBPUSD|True|4|100000.0|1e-05|0.01|0|0|1792174233
USDJPY|True|4|100000.0|0.001|0.01|0|2|1792174233
USDCAD|True|4|100000.0|1e-05|0.01|0|1|1792174233
USDCHF|True|4|100000.0|1e-05|0.01|0|1|1792174233
AUDUSD|True|4|100000.0|1e-05|0.01|0|1|1792174233
NZDUSD|True|4|100000.0|1e-05|0.01|0|1|1792174233
EURGBP#|False|None|None|None|None|None|None|None
EURJPY#|False|None|None|None|None|None|None|None
GBPJPY#|False|None|None|None|None|None|None|None
BTCUSD#|False|None|None|None|None|None|None|None
ETHUSD#|False|None|None|None|None|None|None|None
US500Cash#|False|None|None|None|None|None|None|None
US30Cash#|False|None|None|None|None|None|None|None
US100Cash#|False|None|None|None|None|None|None|None
UK100Cash#|False|None|None|None|None|None|None|None
EURGBP|True|4|100000.0|1e-05|0.01|0|3|1792174233
EURJPY|True|4|100000.0|0.001|0.01|0|3|1792174233
GBPJPY|True|4|100000.0|0.001|0.01|0|7|1792174233
BTCUSD|False|None|None|None|None|None|None|None
ETHUSD|False|None|None|None|None|None|None|None
US500Cash|False|None|None|None|None|None|None|None
US30Cash|False|None|None|None|None|None|None|None
US100Cash|False|None|None|None|None|None|None|None
UK100Cash|False|None|None|None|None|None|None|None
XAUUSD|True|4|100.0|0.01|0.01|0|43|1792174233
XAUEUR|True|4|100.0|0.01|0.01|0|13|1792174233
XAUAUD|True|4|100.0|0.01|0.01|0|40|1792174233
XAGUSD|True|4|5000.0|0.001|0.01|0|8|1792174233
XAUUSD#|False|None|None|None|None|None|None|None
XAGUSD#|False|None|None|None|None|None|None|None
XTIUSD|False|None|None|None|None|None|None|None
XBRUSD|False|None|None|None|None|None|None|None
AUDJPY|True|4|100000.0|0.001|0.01|0|4|1792174233
CHFJPY|True|4|100000.0|0.001|0.01|0|7|1792174233
AUDNZD|True|4|100000.0|1e-05|0.01|0|0|None
GBPCHF|True|4|100000.0|1e-05|0.01|0|0|None
GBPCAD|True|4|100000.0|1e-05|0.01|0|0|None
EURAUD|True|4|100000.0|1e-05|0.01|0|0|None
EURCHF|True|4|100000.0|1e-05|0.01|0|0|None
NZDJPY|True|4|100000.0|0.001|0.01|0|0|None
GBPAUD|True|4|100000.0|1e-05|0.01|0|0|None
AUDCAD|True|4|100000.0|1e-05|0.01|0|0|None
CADJPY|True|4|100000.0|0.001|0.01|0|0|None
NVDA|True|4|1.0|0.01|1.0|5|2|1792173333
USTEC|True|0|1.0|0.01|0.1|0|90|1792174233
US500|True|0|1.0|0.01|0.1|0|30|1792174233
US30|True|0|1.0|0.01|0.1|0|70|1792174233
UK100|True|0|10.0|0.1|0.1|0|11|1778854552
DE40|True|0|1.0|0.01|0.1|0|0|None
US500M|True|0|10.0|0.1|1.0|100|0|None
US30M|True|0|0.1|1.0|1.0|5|0|None
USTECH100M|True|0|1.0|0.1|1.0|100|0|None
```

## Approved whitelist

These 16 live-probed FULL symbols are configured under
`trading.tradable_symbols` and `symbols.whitelist`:

```yaml
trading:
  tradable_symbols:
    - EURUSD
    - GBPUSD
    - USDJPY
    - USDCAD
    - USDCHF
    - AUDUSD
    - NZDUSD
    - EURGBP
    - EURJPY
    - GBPJPY
    - AUDJPY
    - CHFJPY
    - XAUUSD
    - XAGUSD
    - XAUEUR
    - XAUAUD

symbols:
  whitelist:
    forex:
      - EURUSD
      - GBPUSD
      - USDJPY
      - USDCAD
      - USDCHF
      - AUDUSD
      - NZDUSD
      - EURGBP
      - EURJPY
      - GBPJPY
      - AUDJPY
      - CHFJPY
    metals_commodities:
      - XAUUSD
      - XAGUSD
      - XAUEUR
      - XAUAUD
    crypto: []
    indices: []
```

### Three explicit legacy-name mappings

| Previous configured name | Approved broker name | Probe |
|---|---|---|
| `EURGBP#` | `EURGBP` | `exists=True`, `trade_mode=4` |
| `EURJPY#` | `EURJPY` | `exists=True`, `trade_mode=4` |
| `GBPJPY#` | `GBPJPY` | `exists=True`, `trade_mode=4` |

The `#` forms are absent; the plain equivalents are FULL. These three
explicit mappings are recorded in `symbols.mapping`. The scan list uses the
approved broker names directly; it does not apply suffix stripping to other
symbols.

### Metals `required: true`

The existing `metals_commodities` whitelist was empty although the verification
rule requires the asset class. It now contains the four quoted FULL symbols,
and `required: true` remains appropriate.

Crypto and indices remain empty and are `required: false` in verification,
with comments in `config.yaml` explaining that no symbols from those classes
are currently approved. `USTEC`, `US500`, `US30`, and `UK100` exist but are
DISABLED (`trade_mode=0`). The nine other FULL crosses without tick objects in
this probe are intentionally excluded: `AUDNZD`, `GBPCHF`, `GBPCAD`,
`EURAUD`, `EURCHF`, `NZDJPY`, `GBPAUD`, `AUDCAD`, and `CADJPY`. The scan
interval remains 60 seconds. `NVDA` is FULL but is not an approved addition.

The live-resolution test in `tests/test_xm_symbols.py` checks every configured
whitelist symbol with `symbol_info()` when a terminal is available; it skips
when MetaTrader5 or a terminal is unavailable.
