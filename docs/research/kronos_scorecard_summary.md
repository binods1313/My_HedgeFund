# Kronos Scorecard — Results (2026-11-04)

Expanded reproducible run. Protocol: real MT5 M15 bars, context 256, horizon
8 bars, 8 Monte Carlo samples, step 48, ~77–78 windows per symbol, no
lookahead (completed bars only). Machine-readable detail in
`kronos_scorecard_results.json` (per-window records included there).

## Summary table

| Symbol | Windows | Accuracy | Base rate (UP) | Edge over base | Mean agreement | Date range |
|---|---|---|---|---|---|---|
| EURUSD | 77 | 55.8% | 49.4% | **+5.2pp** | 66.6% | 2026-09-07 → 11-04 |
| GBPUSD | 77 | 61.0% | 59.7% | +1.3pp | 68.3% | 2026-09-07 → 11-04 |
| XAUUSD | 78 | 48.7% | 48.7% | −2.6pp | 73.9% | 2026-08-24 → 11-04 |

Failed/None forecasts: **0** on all symbols. Flat moves excluded: 1 (EURUSD).

## How to read this

- **Edge over base** = accuracy minus the better of (UP rate, DOWN rate).
  Positive = the model beats always betting the dominant direction.
- **Agreement** = fraction of sampled paths whose final close matched the
  mean direction (dispersion proxy, not model confidence).

## Honest interpretation

- **EURUSD** shows a modest, consistent edge (+5.2pp on 77 windows) —
  the only result worth taking seriously.
- **GBPUSD** — the earlier small-sample 83% reading did **not** hold up.
  On 77 windows the edge is +1.3pp (noise-level). Do not trust the early read.
- **XAUUSD** — no edge (−2.6pp); accuracy equals the base rate exactly.

## Recommendation

**Keep Kronos observational only — do NOT enable it as a gate yet.**

Reasons:
1. Only EURUSD shows a real edge, and +5pp is modest; it needs to survive
   out-of-sample/forward confirmation on more data before it gates anything.
2. Agreement is not predictive power — all three symbols show high agreement
   (66–74%) even where there is no edge.
3. `kronos.enabled` stays `false` by default. The observational hook is in
   place: once enabled, forecasts are journaled beside signals without
   gating, building the forward paper track record.

Next step if you want to pursue it: enable `kronos` observationally
(`enabled: true`), let it journal beside real paper signals for 2–4 weeks,
then re-score against the live signal log. Enable as a gate only if EURUSD
(and ideally others) shows a **stable** positive edge on that forward sample.
