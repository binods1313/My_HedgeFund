## 13. TASK 10 — Evidence gaps, gated implementation, UI-only upgrades. Raw output for every item.
Scope as before: no engine/ edits unless a flag below says YES, no config.yaml, no submit/close behaviour changes, never print secrets, no new Python dependencies, explicit-path commits, do not edit docs/Instructions*.md.

OPERATOR APPROVALS (the operator edits these before running; treat anything not YES as NO):
APPROVE_REALISED_PNL_MIGRATION: NO
APPROVE_STRATEGY_LIBRARY_AND_BACKTEST: NO
APPROVE_PENDING_ORDERS_PAPER: NO

PART A — EVIDENCE FOR TASK 9 (read-only, paste raw output)
A1 pytest summary line, secret scan result, git diff c54fa2d..HEAD --stat -- engine/ with one line per file.
A2 Line 5 of docs/Instructions_2.md with every value after "=" or ":" replaced by ****, and your classification (real secret / example text / false positive). Report whether the worktree test is now green and how.
A3 The import-graph result proving learning/, engine/research/, dexter_client and infoway_client are unreachable from the order path, and the name of the test that enforces it.
A4 For each of B1 to B9 in Task 9: DONE / PARTIAL / NOT DONE, the commit hash, the test name. Include what the header shows with no high-impact event, where the Ideas watchlist lives, and whether the Enable AI control exists and how it is disabled in LIVE.
A5 Regression tests (Kilo round 5 verified these manually): AppTest renders the Portfolio view once and asserts the header strip and Portfolio section equity and P&L are identical; Theme Studio invalid hex shows an inline error after commit, Reset and Export controls exist, and a low-contrast colour such as #101010 shows the contrast ratio and the adjusted value (Kilo saw no warning for #101010: find out why, fix it, test it).

PART B — GATED WORK (do only parts whose approval flag is YES; otherwise write "skipped: not approved")
B1 If APPROVE_REALISED_PNL_MIGRATION is YES: implement docs/design/realised_pnl.md as written (forward-only journal migration, PaperExecutor and LiveExecutor recording, backfill rules, tests with fake_mt5). This is the only engine/ change allowed in this task; list every engine file touched and prove with tests that risk limits, gates and order flow are unchanged. Then wire learning/trades_link to it.
B2 If APPROVE_STRATEGY_LIBRARY_AND_BACKTEST is YES: implement docs/design/strategy_library_and_backtest.md (declarative strategies as data, vectorised backtest with no look-ahead, costs, SL-first rule, 70/30 walk-forward, locally computed rating with minimum trade counts, Top 3 by out-of-sample result, "Signal agreement" score never labelled probability or confidence, "Available for Trading (candidate)" badge only above the configured agreement threshold and with at least 30 backtest trades; it must never bypass risk, the validator, approval or the executor). UI only: "Quantitative Backtest" view with the parameter form, metrics ribbon, equity, drawdown and monthly heatmap, CSV export, hypothetical-results disclaimer. Tests per the design doc.
B3 If APPROVE_PENDING_ORDERS_PAPER is YES: implement docs/design/pending_orders.md for PAPER mode only; LIVE stays disabled. Same proof requirements as B1.

PART C — UI ONLY (no approval needed)
C1 Interactive cards: Broker tick volume, Margin allocation, Engine equity curve each get a view toggle (bar / line / donut where it makes sense), hover stats, and colour coding with a text label (safe/caution/risk, never colour alone). Each card has an inline SVG function icon and its own slow clockwise or anticlockwise motif; prefers-reduced-motion stops it. Contrast tests extended.
C2 Performance dashboard view section: equity-based metrics only until realised P&L exists: equity change over a selectable window, max drawdown, and Sharpe computed from equity snapshots with the annualisation documented and "—" when fewer than 30 points. Label clearly "from equity snapshots". Win/loss ratio appears only when realised P&L exists.
C3 Risk alerts: keep the banner; add a sound-free, dismissible notice when margin level is below the config warn level.
C4 Chart style "Bar" currently renders solid red/green columns on the price pane. It must draw OHLC bars (open tick left, close tick right) or be removed. Test the figure builder for each style (Candlestick, Area, Bar) and assert the trace type.
C5 Engine equity curve: show y-axis tick labels, a caption with the € and % change over the window, and a minimum visible y-range (e.g. at least 0.05% of equity) so a small fluctuation does not look like a crash. Test the scaling function.
C6 Narrow screens (390px): nav labels and header metric labels must not be truncated (wrap, abbreviate or stack the nav as a 2-column grid); no horizontal page scroll. Extend the markup tests where feasible and list the checks for Kilo.
C7 FOMC / news chip: when no High-impact event exists within the window, show "No high-impact event in the next 24h" in the same slot with no leftover gap. Test both states.

PART D — GATES
D1 pytest summary line, secret scan, git diff --stat, commits, git status --short.
D2 Update docs/PROGRESS.md and docs/ui.md; list what you could NOT do; ranked open-risk table.
THEN STOP.