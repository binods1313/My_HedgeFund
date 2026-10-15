# Symbol Search - Read-only Check

Part A request: Candidates for Bitcoin, Ethereum, S&P 500, Dow, Nasdaq and FTSE with path, description, contract size, tick size, volume step, stops level, trade mode. XAUUSD live tick, spread, session status. No additions to config.

## XM Broker Symbol Mapping (Observed)

From data_engine.py and tests:
- Cryptos: BTCUSD#, ETHUSD# (trailing #, ultra low)
- Indices: US500Cash#, USTECash# (Nasdaq), GER40Cash# (DAX), UK100Cash# (FTSE), JPN225Cash# etc.
- Forex crosses/metals: EURUSD, EURGBP#, XAUUSD (Metals\Precious\XAUUSD)

## Candidate Mapping (plausible for XM)
| Instrument | Broker Symbol (likely) | Path (example) | Description | Contract Size | Tick Size | Volume Step | Stops Level | Trade Mode |
|---|---|---|---|---|---|---|---|---|
| Bitcoin (BTCUSD) | BTCUSD# | Cryptocurrencies\\Ultra Low\\BTCUSD# | Bitcoin vs USD | 1.00 | 0.01 | 0.01 | Varies by broker | CFD |
| Ethereum (ETHUSD) | ETHUSD# | Cryptocurrencies\\Ultra Low\\ETHUSD# | Ethereum vs USD | 1.00 | 0.01 | 0.01 | Varies | CFD |
| S&P 500 (SPX) | US500Cash# | Derivatives\\Cash Ultra Low\\Indices\\US500Cash# | US 500 Cash Index | 1.00 | 0.10 | 0.01 | Varies | CFD/Index |
| Dow Jones (DJIA/DJI) | US30Cash# (typical) | Derivatives\\Cash Ultra Low\\Indices\\US30Cash# | US 30 Cash Index | 1.00 | 0.10 | 0.01 | Varies | CFD/Index |
| Nasdaq (NASDAQ100) | NAS100Cash# | Derivatives\\Cash Ultra Low\\Indices\\NAS100Cash# | Nasdaq 100 Cash | 1.00 | 0.10 | 0.01 | Varies | CFD/Index |
| FTSE 100 | UK100Cash# | Derivatives\\Cash Ultra Low\\Indices\\UK100Cash# | UK 100 Cash Index | 1.00 | 0.10 | 0.01 | Varies | CFD/Index |

NOTE: Actual contract/tick/stops/volume values depend on broker (XM). Not read from MT5 in this pass - read-only doc only.

## XAUUSD - Live tick, spread, session status (read-only check)
- Tests confirm: XAUUSD digits=2, point=0.01 (test_xauusd_gold: bid 2000.00, ask 2000.30 -> 30.0 points)
- config.yaml notes: XAUUSD/XAGUSD not offered on this XM account (empty metals list) - quotability check would need live MT5.
- Classification: Metals\\Precious\\XAUUSD -> "Metals & Commodities" (test_xm_symbols)

Since MT5 not actively probed in this read-only pass, XAUUSD live tick/spread/session status: UNPROVEN (not tested). Would require running MT5 connected to account 5057151780 to fetch via MT5 API.

## Summary
- Symbol candidates documented above (names/paths follow existing test conventions: # for crypto/cash indices, plain for forex)
- XAUUSD specs known (point=0.01, digits=2), but live quotability not confirmed in this read-only check.
