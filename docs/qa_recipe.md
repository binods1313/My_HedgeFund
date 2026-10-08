# QA recipe — Playwright MCP

Drive the dashboard that is already on `http://127.0.0.1:8501`. Do not start a second Streamlit. Do not click Submit, Close, or Emergency close all. This file is the recipe. It is not a test run.

The live process may be older than the latest commit. If a control from this recipe is missing, say the build is stale and stop. Do not restart the server unless the operator asked.

## How to drive it

Connect Playwright MCP with newline-delimited JSON-RPC. Do not use Content-Length framing. Do not call `browser_close` (it has hung). Terminate the client process in a `finally` block.

Wait for a rerun after each click: the button's pressed state settles and the target heading or metric is visible. A single screenshot is not a check. Read the accessible name and the metric value.

Prefer, in this order:

1. `data-testid` when Streamlit emits one (`stButton`, `stSelectbox`, `stMultiSelect`, `stButtonGroup`).
2. The `st-key-*` class Streamlit adds for a widget `key`.
3. Role plus accessible name.

## Navigation

Click the main nav button. Do not open the Facts popover to change pages. The Facts control is a popover labelled `Facts`. Reading it is allowed. Using it as the only way to change tabs is not.

| Page | Button key | Accessible name | Stable class |
| --- | --- | --- | --- |
| Exchange | `nav-exchange` (active key `nav-exchange-on`) | `🌐 Exchange` | `st-key-nav-exchange` |
| Portfolio | `nav-portfolio` / `nav-portfolio-on` | `💼 Portfolio` | `st-key-nav-portfolio` |
| AI Signals | `nav-signals` / `nav-signals-on` | `🤖 AI Signals` | `st-key-nav-signals` |
| Live Market News | `nav-news` / `nav-news-on` | `📰 Live Market News` | `st-key-nav-news` |
| Journal | `nav-journal` / `nav-journal-on` | `🧾 Journal` | `st-key-nav-journal` |
| Research Desk | `nav-research` / `nav-research-on` | `🔬 Research Desk` | `st-key-nav-research` |

The active page also exposes a visually hidden `Current page` span with `aria-current="page"`. Nav buttons are in tab order and use `button:focus-visible` in `ui/theme.py`.

## Other stable controls

| Control | Locator |
| --- | --- |
| Ideas watchlist | container key `ideas-watchlist` → class `st-key-ideas-watchlist`, heading `Ideas watchlist` |
| Theme Studio | key `btn_theme_studio`, name `🎨 Theme Studio` |
| News toggle | key `w_news_affects_open`, name `Affects my open positions` |
| Enable AI | key `btn_enable_ai`, name `Enable AI` |
| Market sector | key `w_asset_category` inside `[data-testid="stSelectbox"]` |
| Instrument | key `w_selected_asset` |
| Indicator multiselect | `[data-testid="stMultiSelect"]` |
| Segmented controls (side, order type, timeframe) | `[data-testid="stButtonGroup"] button[role="radio"]` |
| Order side | key `w_order_side` |
| Timeframe | key `w_timeframe` |

Multiselect and segmented controls: focus the control, then use the keyboard (Arrow keys and Space or Enter) rather than clicking a hidden option node. After a keypress, wait for the rerun and read the selected value back. React Aria nodes are `[data-testid="stSelectbox"] [role="group"]`, `[data-testid="stMultiSelect"] [data-tag]`, and `[role="listbox"] [role="option"]`. Do not require `[data-baseweb]` nodes. The stylesheet still carries both.

## Colours and values

Read colours with `getComputedStyle` on the live node. A screenshot that disagrees with the computed style loses. Read money from the metric's accessible value, not from a cropped image. Header `Floating equity` and Portfolio `Equity` are the same snapshot and should match to the cent, including the thousands separator. `Open P&L` should match the same way. The currency symbol comes from the account currency.

The high-impact chip is the `.news-countdown` span. Inside 24 hours it contains the event title. Otherwise the text is `No high-impact event in the next 24h`. An empty gap where that span should be is a failure.

## Safe actions

These do not send an order:

- Click a main nav button.
- Open Facts and read the lines. Close it without clicking `Open …` if you are only reading.
- Change Market sector, instrument, timeframe, or the indicator multiselect.
- Open Theme Studio, type, and press Escape or click away. Do not Import a file you did not create for the check.
- Toggle `Affects my open positions` and `Show past`.
- Open the Ideas watchlist section and read the table.
- Read the Enable AI button. In LIVE it is disabled and the caption contains `Disabled in LIVE mode`.

## Never click

- `Submit` (`btn_submit_order`) and `Confirm` (`btn_confirm_order`).
- Any `Close` button whose key starts with `close_`.
- `Emergency close all` (`btn_close_all`).
- `Confirm Enable AI` (`btn_enable_ai_confirm`). Looking at the disabled LIVE button is the check. Confirming would queue `ENABLE_AI`.
- Any dialog whose title is `Confirm order`.

If a click lands on one of those, stop and say so. Do not confirm a second dialog to "undo" it.

## Per view

Exchange. Heading area shows the selected symbol. The chart is in the wide column. Ideas watchlist is under the chart and the secondary-data panel, inside `st-key-ideas-watchlist`. The order form is in the narrow column. Submit stays disabled when the broker is disconnected. Limit and conditional stay disabled with the caption `engine does not support this yet`. Pattern marks are shapes. Hover one for the short code. They are not required to show a long name on the candle.

Portfolio. Click `💼 Portfolio`. The view title is `Portfolio`. Equity and Open P&L match the header metrics from the same run, to the cent. There is no second number from a different fetch.

AI Signals. The reactor shows BUY, SELL, or HOLD. Confidence text says it is a display value and not an execution gate. Do not treat it as an order.

Live Market News. The toggle `Affects my open positions` is visible. With it on, an event whose currency is not one of an open position's currencies drops off, and an `ALL` event stays. With it off, the currency filter from that toggle is not applied. Event titles are text. A title that contains a script tag is shown escaped.

Journal. The table is read-only. Relative times have a title tooltip with the absolute timestamp.

Research Desk. The first sentence is `Shadow mode: these outputs do not affect trading.` Agent cards do not add a confidence the ledger did not compute. Approve, Reject, Apply, and Rollback open a confirm dialog. Do not confirm Apply.

Enable AI. PAPER: the button is enabled and nothing is queued until `Confirm Enable AI`, which this recipe does not click. LIVE: the button is disabled and the caption is `Disabled in LIVE mode. Paper mode only.`

Theme Studio. An invalid hex such as `BADHEX` shows an inline error that names `#RRGGBB` and does not change the saved palette. A contrast adjustment warning includes `Contrast`, a ratio ending in `:1`, and the adjusted `#RRGGBB`.
