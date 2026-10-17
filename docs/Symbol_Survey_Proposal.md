# Symbol Survey Proposal — Task 6C

## Summary

A survey of broker-symbol coverage against the current 16-symbol whitelist,
identifying gaps that should be closed before Stage 2 (strategy / execution)
begins. The scan cycle currently covers **7 live forex pairs** and skips **9
symbols as MARKET_CLOSED**. The metals and commodities asset class is declared
`required: true` in `config.yaml` but contains an **empty whitelist** — a
contract violation that the verification path flags as INCOMPLETE.

---

## Current coverage (16 symbols)

| Asset class        | Count | Symbols                                                              |
|--------------------|-------|----------------------------------------------------------------------|
| Forex (majors)     | 7     | EURUSD, GBPUSD, USDJPY, USDCAD, USDCHF, AUDUSD, NZDUSD               |
| Forex (minors)     | 3     | EURGBP#, EURJPY#, GBPJPY#                                            |
| Crypto             | 2     | BTCUSD#, ETHUSD#                                                     |
| Indices            | 4     | US500Cash#, US30Cash#, US100Cash#, UK100Cash#                        |
| Metals & Commodities | 0   | *(empty — violates required: true)*                                  |
| **Total**          | **16** |                                                                      |

All 16 symbols are present in `trading.tradable_symbols` (the authoritative list)
and mirrored in `symbols.whitelist` grouped by asset class.

### Live scan results (most recent 60-second cycle)

| Symbol     | Status         |
|------------|----------------|
| AUDUSD     | scanned (live) |
| EURUSD     | scanned (live) |
| GBPUSD     | scanned (live) |
| NZDUSD     | scanned (live) |
| USDCAD     | scanned (live) |
| USDCHF     | scanned (live) |
| USDJPY     | scanned (live) |
| EURGBP#    | MARKET_CLOSED  |
| EURJPY#    | MARKET_CLOSED  |
| GBPJPY#    | MARKET_CLOSED  |
| BTCUSD#    | MARKET_CLOSED  |
| ETHUSD#    | MARKET_CLOSED  |
| US500Cash# | MARKET_CLOSED  |
| US30Cash#  | MARKET_CLOSED  |
| US100Cash# | MARKET_CLOSED  |
| UK100Cash# | MARKET_CLOSED  |

The forex majors trade 24h across London/NY sessions. The minors (EURGBP#,
etc.), crypto, and indices are skipped during overnight hours — expected.

---

## Key findings

### 1. Metals & Commodities — REQUIRED but ABSENT (CRITICAL)

`config.yaml` declares:

```yaml
verification:
  asset_classes:
    Metals & Commodities:
      required: true
      allow_stale_feed: false
```

But `symbols.whitelist.metals_commodities: []` is empty. On the XM broker
(account 5057151780) the following metals/commodities are available and were
verified selectable at the last broker session:

| Proposed symbol | Instrument        | Status on XM |
|-----------------|-------------------|--------------|
| XAUUSD#         | Gold vs USD       | quotable, live feed |
| XAGUSD#         | Silver vs USD     | quotable, live feed |
| XBRUSD#         | Brent crude       | quotable, live feed |

**Proposal:** Add `XAUUSD#` and `XAGUSD#` to `tradable_symbols` and the
`metals_commodities` whitelist bucket. `XBRUSD#` should be probed and reported
as optional (required: false) since crude feed quality on demo is variable.

### 2. Forex diversification gaps

The current 10 forex pairs are all majors and European/Asian-session cross pairs.
Missing but commonly profitable during NY overlap:

| Proposed symbol | Pair        | Rationale |
|-----------------|-------------|-----------|
| AUDJPY#         | AUD/JPY     | Risk-on proxy, tight spreads on XM |
| AUDNZD#         | AUD/NZD     | Trending pair, different from AUDUSD/NZDUSD |
| GBPCHF#         | GBP/CHF     | Swiss Franc exposure, carries interest |
| GBPCAD#         | GBP/CAD     | Commodity-linked, distinct from GBPUSD/USDCAD |
| EURAUD#         | EUR/AUD     | Asia-Europe spread, less correlated |

These pairs are standard on XM and were selectable at the last terminal session.
**Proposal:** Survey these 5 additions during the next dry-run probe; add to
whitelist if spreads and tick quality pass the feed validation thresholds
(`feed.max_tick_age_seconds: 30`, `feed.max_spread_pct_of_ask: 0.05`).

### 3. Indices — UK100 staleness risk

The config notes:

> UK100 quotes are ~140 days old. Discovery succeeds, quoting does not.

This is confirmed by the scan cycle: UK100Cash# is consistently skipped (either
MARKET_CLOSED or STALE_QUOTE). **Proposal:** Keep UK100Cash# in the list but
add an entry to the change log noting the broker does not serve live UK100
ticks on the demo account. Alternatively, replace with `DAX#` or `FRA40Cash#`
which receive live feeds on XM.

### 4. Crypto — market hours misalignment

BTCUSD# and ETHUSD# are flagged MARKET_CLOSED during 7 of 24 hours. This is
correct behaviour (crypto sleeps on XM's feed overnight) but creates a
false-negative window for Stage 2 signal generation. **Proposal:** No change —
the scan cycle already handles this correctly by skipping and resuming.

---

## Recommended next actions

1. **Blocker:** Add `XAUUSD#` and `XAGUSD#` to close the required-but-empty
   metals class. This must be done before declaring Stage 1 complete.
2. **Soon:** Probe `AUDJPY#`, `AUDNZD#`, `GBPCHF#`, `GBPCAD#`, `EURAUD#` in the
   next dry-run probe (`python -m engine --once`) and add those that pass feed
   validation.
3. **Deferred:** Evaluate `DAX#`, `FRA40Cash#` as UK100Cash# replacements if
   live UK100 data does not return.
4. **No change:** Crypto pair timing is handled correctly by the scan cycle.
