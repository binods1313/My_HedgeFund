ROLE: QA via Playwright MCP against http://localhost:8501. Report only. Output folder: E:\HedgeFund_Binod_v2\reports\ui_qa\round5\.
SCREENSHOTS: the Playwright tool can only write under C:\Users\Binod\.playwright-mcp. Save there, then copy each file into round5\ with a shell command and list the final paths. Do not claim a screenshot you did not copy.
STEP 0: read docs/qa_recipe.md and quote the 5 locators you will use. Restart the process on 8501 only (never 8502/8503); record PID and start time. Do not touch the engine.
SAFETY: never click Submit, per-row Close, Emergency close all, Enable AI, or confirm any order dialog. Never read or print .env or keys. Mask account numbers.
RULES: after typing in any text field press Enter, then wait for the rerun. Scroll inside dialogs to the bottom. A PASS needs a recorded value or a cropped screenshot of the exact element. Reading page text alone is not enough for visual checks.
CHECKS:
1 Risk card: crop the alert element showing the validation code; record the alert's own computed background and text colour (not the card container). Must be amber or red while a code shows.
2 Margin donut: crop the donut at 1920, 1280 and 390; judge whether the centre text fits inside the ring.
3 Equity curve: crop the chart; record y-axis labels, first and last visible value, any vertical drop.
4 Header vs Portfolio: stay on the Portfolio view, where the header strip and the Portfolio section are both visible. In ONE evaluate call read both equity values and both P&L values; repeat 3 times 6 s apart. Report the six pairs.
5 Theme Studio: type BADHEX in a hex field, press Enter, record any inline error text; type a low-contrast colour such as #101010 and press Enter, record the ratio and adjusted value; scroll the dialog bottom and record whether Reset and Export JSON exist; click Reset; Export JSON keys only.
6 FOMC chip: record whether the header shows it and what text appears when there is no high-impact event; describe any empty gap with a crop.
7 Ideas watchlist: where it is reached from the UI (exact clicks); entry with <b>x</b> shown escaped.
8 Enable AI control: locate it, record that it is disabled or guarded in LIVE mode and shows a confirmation dialog; cancel it, never confirm.
9 Research Desk: 10 agent cards with statuses (list them), Dexter fixture badge, Infoway chip text, shadow banner.
10 Pattern marks: survive a symbol change and a timeframe change and a reload (three screenshots).
OUTPUT: report.md table (ID, result, recorded value, copied screenshot path), ranked defects with reproduction steps, could-not-test list with what you tried. Paste both lists in your reply.