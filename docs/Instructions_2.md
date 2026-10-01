# REVIEW OF V2_Implementation_Plan.md: REQUIRED CHANGES BEFORE IMPLEMENTATION

Good structure and correct Phase 0 diagnosis. However, several code snippets in the plan contain bugs that would cause real losses, and parts of the original brief are missing. Update the plan and fix everything below. Answer to your question: yes, start Phase 0, under the conditions in section 6.

---

## 0. SECURITY (do immediately)

- The plan document contains the real MT5 password. Remove ALL real credentials from the doc and from any committed file. Use placeholders only.
- Add `.env` to `.gitignore`. Check git history for leaked secrets and scrub them.
- I will rotate the MT5 password and the OpenRouter key. Code must read secrets from environment variables only.

---

## 1. MUST-FIX BUGS IN THE PROPOSED CODE

**1.1 Trade Manager: R-multiple is unsigned**
`profit_r = abs(price_current - entry) / sl_dist` treats a losing trade at -1R as +1R and would move it to breakeven.
Fix: signed R. BUY: `(price - entry) / initial_sl_dist`. SELL: `(entry - price) / initial_sl_dist`.
Store the **initial SL distance immutably** in the journal at entry, since R-multiples depend on it after SL is moved.

**1.2 Minimum-lot check is unreachable (Bug 1 and `calculate_lot`)**
`max(volume_min, ...)` runs before the `< volume_min` check, so it can never fire. A $100 account on BTCUSD would round UP to min lot and break the risk rule.
Fix order: compute `raw_lot` → floor to `volume_step` → if `raw_lot < volume_min` return reject (do NOT clamp up) → then clamp to `volume_max`.
Avoid float floor errors (0.07/0.01 = 6.999...). Use `Decimal` or round to the step's decimal places.
Also use `order_calc_profit` (or the loss-side tick value) for loss-per-lot at the SL, and run `order_check` before sending.

**1.3 MT5 Gateway**
- On timeout it returns `None` silently, but the order may still execute later (ghost/duplicate orders). Raise `TimeoutError`, then reconcile against `positions_get` / `history_orders_get` before any retry.
- `mt5.initialize()` and `login` must run inside the gateway thread.
- Add auto-reconnect with backoff; trading is paused while disconnected.

**1.4 Take-profit conflicts with Trade Manager**
Strategy sets TP at 2R, but the manager closes 50% and starts trailing at 2R, so the position is closed at TP before the trail starts.
Fix: TP at 3R or higher, or no fixed TP when trailing is enabled. Make it configurable per strategy.

**1.5 LLM can create trades**
`confidence_adjust` of +20 can lift a 70 score above the 85 threshold, so the LLM becomes the decision maker again.
Fix: the LLM may only **reduce** confidence (adjust range -20 to 0) or REJECT/REDUCE size. Apply the execution threshold AFTER the LLM step.
Replace `assert` with explicit validation (pydantic). `assert` is stripped under `python -O`.
Reject LLM output whose cited numbers deviate from the indicator snapshot.

**1.6 Confidence Engine is incomplete and wrong**
- Trend and momentum scores are not direction-aware (RSI 20 on a BUY would score high). Scores must be computed relative to the signal direction.
- The `* 1000` scaling is price-dependent. Normalize by ATR.
- Volatility, volume and structure scores are not implemented, and missing keys silently count as 0 (`scores.get(k, 0)`). Implement all five, with documented formulas, and raise an error on missing keys.
- Note: forex tick volume is a weak proxy. Make the weight configurable per asset class.

**1.7 Scanner**
- 100 bars is not enough for EMA200. Fetch at least 300 bars.
- It does not fetch the 4H dataframe, but `Strategy.evaluate(df_entry, df_htf)` requires it.
- `StrategyEngine.score_all(df)` does not match that signature.
- Watchlist is hardcoded. Read it from `config.yaml`, with a broker symbol-mapping table (suffixes like `.m`, `#`).
- Only recompute on a new closed bar, and cache candles.

**1.8 Risk Engine**
- `_max_drawdown` and `_margin_check` are called but never defined.
- Missing: max total exposure (sum of SL-risk as % of equity), correlation filter, news filter, session/market-hours filter.
- Open-trade count must come from **MT5 positions** (source of truth), not the journal.
- Hitting a limit only rejects individual trades. Per the brief, it must move the engine to `PAUSED` with a reason. Daily-loss pause resets next trading day; drawdown pause requires manual resume.

**1.9 Symbol classification**
Anything not matched by path must be **non-tradable until whitelisted** in config, not "Other".

**1.10 Lightning border CSS**
The `::before` conic-gradient fills the whole card. Use a mask or an inner background layer so only the border glows and text is unaffected.

---

## 2. MISSING FROM THE PLAN (required by the original brief)

1. **UI to engine command channel.** Streamlit no longer touches MT5, so add a `commands` table (command, params, status, ts). ENABLE AI, Pause, Resume, Close position, Emergency Close All and config changes all go through it. The engine polls it and writes results back.
2. **Engine to UI data.** The engine must write quotes, positions, account info, chart candles/indicators, scan results and risk alerts to the DB so the UI can render without MT5.
3. **PAPER mode.** A simulated executor with the same interface as the live one, used by default. LIVE requires a config flag plus typed confirmation in the UI. The badge is always visible.
4. **Backtesting Center** (no section exists). Bar-by-bar replay using the SAME strategy and risk code as live. Include spread, commission, slippage and swap. Walk-forward / out-of-sample validation. Report: net profit, PF, win rate, max DD, Sharpe, trade list, equity curve.
5. **Performance Analytics** (the plan's "Phase 9" is the LLM, not analytics). Win rate, profit factor, avg R:R, Sharpe, current and max drawdown, monthly return, trades today, AI accuracy. State the formulas in code comments.
6. **UI panels:** AI CORE (animated ring: status, confidence, trades today, profit today, scanner status), Opportunity Scanner (Top 10), AI Trade Manager (protected / breakeven / trailing / risk alerts), AI JOURNAL.
7. **Reliability:** reconciliation of MT5 positions (by magic number) on engine restart, idempotency (one trade per symbol per signal bar), reconnect handling, `retcode` checks on every order, correct `type_filling` per symbol, and `stops_level` / freeze-level handling on SL modification.
8. **Alerts** (Telegram or email) for trades, pauses and errors.
9. **`config.yaml` schema** with documented defaults and validation on load (symbols, per-symbol max spread, risk limits, weights, thresholds, sessions, news provider).
10. **Smart Money strategy** and Momentum / Breakout strategies (only Trend is shown). Document deterministic detection rules for FVG, order block, liquidity sweep and MSS, and unit-test them on fixed fixtures.
11. **Extra DB tables:** `signals`, `scan_results`, `events`, `commands`. Version the schema migrations and back up the DB daily.

---

## 3. CONSISTENCY FIXES

- Phase numbers in the section headings do not match the deliverables table (e.g. scanner is "Phase 2" in the body but delivered in Phase 5). Use one numbering scheme.
- **Build order must be:** Phase 0 → engine skeleton + gateway → Risk Engine + Position Sizing → Journal DB → Strategies + Confidence → Scanner → LLM validator → ENABLE AI autonomous loop → Trade Manager → Analytics + Backtesting → UI polish. Autonomy must never ship before the risk gates exist.
- Confidence threshold is "> 85" in the brief, so use strictly greater and make it configurable per asset class. Log the score distribution so it can be calibrated from data.

---

## 4. TESTS REQUIRED

- Position sizing: EURUSD, USDJPY, XAUUSD, NAS100, BTCUSD (small account, must return reject), plus a cross-currency account.
- Lot normalization: step rounding, min/max, float edge cases (0.07).
- Risk Engine: every limit, pause transitions, resume rules.
- State machine transitions.
- Strategies on fixed fixtures (closed bars only, no look-ahead).
- LLM validator: malformed JSON, timeout, out-of-range values, number-mismatch rejection. All must result in NO TRADE.
- Trade Manager: signed-R logic (a losing trade must never trigger breakeven), partial close respecting volume_min/step.
- Integration test on MT5 demo: place, modify SL, partial close, close.

---

## 5. NON-NEGOTIABLE SAFETY RULES

1. Default mode is PAPER/DEMO.
2. No code path sends an order without Risk Engine APPROVED.
3. Every order has an SL attached at placement (broker-side).
4. Any error, timeout or ambiguity means NO TRADE.
5. Emergency Stop always works, halts the scanner and blocks new orders.
6. Minimum 2-4 weeks demo burn-in with journal review before any live use.

---

## 6. NEXT STEP

Start Phase 0 now, with these conditions:
- Work on a separate git branch, one commit per bug.
- Include unit tests for lot normalization and spread calculation.
- Apply the corrected logic from 1.2 (not the version in the plan).
- Verify on the demo account and send me the results (before/after) before starting Phase 1.
- Then send me the updated plan with sections 1-3 incorporated.