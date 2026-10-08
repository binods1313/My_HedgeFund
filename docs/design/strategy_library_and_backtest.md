# Strategy library and backtest — design only

This document does not add a backtester, a ranker, or a strategy runner. Nothing here is wired to orders. Agent cards stay advisory. The hub line stays `Agent evaluation: not evaluated`.

## Rules are data

A strategy is a JSON document stored under `data/strategies/` (gitignored once it exists). It is a list of named comparisons against series the chart already computes: EMA20, EMA50, RSI, MACD, ATR, Bollinger. There is no seventh indicator.

Example shape, not an implementation:

```json
{
  "name": "ema-cross-rsi",
  "symbol": "EURUSD",
  "timeframe": "H1",
  "rules": [
    {"series": "EMA20", "op": "above", "other": "EMA50"},
    {"series": "RSI", "op": "below", "value": 70}
  ]
}
```

The loader accepts only those fields. Unknown keys are rejected. `op` is an enum (`above`, `below`, `crosses_above`, `crosses_below`). Numbers are finite. The symbol must be on the existing whitelist. The document is not code. The loader must not `exec`, `eval`, or import a path from the file. It must not download a strategy. A file that contains a string meant to be run is rejected.

## Backtest

Vectorised on closed bars the repo already stored. The signal is computed on bar `t` using only bars at or before `t`. The simulated fill is the next bar's open, plus cost. Using bar `t`'s close as both the signal and the fill is look-ahead and is a failed test.

Costs: the bar's spread as a price distance when the bar has one, otherwise the test fixture's spread, plus a commission figure stored on the run. Both are subtracted. A run with costs set to zero must say so in the result. It must not be labelled as a live expectancy.

Intrabar stop and target: when one bar touches both, the stop is first. That is the same rule as `learning/resolver.py`. The backtest must not assume the target was hit because the high and the low both reached.

Walk-forward: sort the closed bars in time, fit nothing on the last 30 percent, and report the first 70 percent and the last 30 percent separately. The last 30 percent is not used to choose the rule thresholds. If a person changes a threshold after seeing the last 30 percent, that run is labelled `peeked` and is dropped from the ranking.

## Rating and Top 3

The rating is computed on this machine from the walk-forward test window. It is not fetched.

A strategy is eligible only when the test window has at least 30 round-trip trades. Fewer than 30 is `INSUFFICIENT`, the same floor the shadow calibration uses, and it cannot be in the Top 3. Among eligible strategies, rank by mean R on the test window after costs, and show the trade count next to the rank. The Top 3 are the three highest eligible names. Ties break by name, ascending, so the order does not depend on file order.

## Signal agreement

Replace any sentence that says a setup has "75% confidence" or any other probability the model did not measure. The number this library may show is signal agreement: how many of the strategy's rules are true on the last closed bar, divided by how many rules it has. A strategy with 3 rules and 2 true is `Signal agreement 2/3`. That is not a probability of profit, and the UI must not append a percent confidence to it.

Agent 08's rule count is a separate shadow output. It is not this score, and it still does not place orders.

## What a backtest can and cannot show

It can show how these declarative rules would have lined up with these stored bars, with the cost and the stop-first rule above, on the held-out 30 percent.

It cannot show that the strategy will make money next week. It cannot show live slippage, a closed market, a rejected order, or a gap that the stored bars did not contain. It cannot turn a high rank into a permission to trade. A rank is a sort of past runs.

Overfitting: many rule combinations on one symbol, a short window, a threshold chosen on the test window, and dropping the losing variants after the fact will manufacture a Top 3. The `peeked` label and the 30-trade floor are the guards. They do not make the rank true out of sample beyond that one split. A second, later window can disagree. The report should say that in one sentence next to the Top 3.

## Review checklist

- [ ] A strategy file is data. Unknown keys fail. No `exec`, `eval`, import, or download.
- [ ] The signal at bar `t` fills at bar `t+1` open.
- [ ] Costs are applied, and a zero-cost run is labelled.
- [ ] A bar that touches stop and target counts the stop.
- [ ] The last 30 percent is not used to pick thresholds. A peeked run is not ranked.
- [ ] Fewer than 30 test trades is `INSUFFICIENT` and is not in the Top 3.
- [ ] The UI says `Signal agreement n/m` and does not say a confidence percent.
- [ ] The Top 3 sentence states that the rank is a past sort, not a forecast.
- [ ] No order path imports the strategy loader.
- [ ] The operator has approved this document before any of it is coded.
