# UI architecture

This dashboard keeps the trading engine as the source of truth and exposes a thin Streamlit UI layer. All live numbers are either pulled from the MT5 broker or from the engine journal; the UI never invents portfolio state.

## Theme tokens

The theme lives in `ui/theme.py` and is mirrored in `.streamlit/config.toml` so the page does not flash a white shell before styling loads.

- Background: `#121318`
- Panel: `#1A1C24`
- Border: `#262A36`
- Text: `#E6E8EE`
- Accent: `#B8F2FF`
- Gain: `#00E676`
- Loss: `#FF3B30`

## State

The session-state defaults are defined in `ui/state.py` and are initialised once at startup via `init_state(whitelist)`. The state is validated against the current whitelist so a config change cannot leave stale asset selections behind.

## Data access

`ui/data_access.py` owns the contract for read-only broker data and order submission:

- `get_account_snapshot()` returns an `AccountSnapshot` built from the live telemetry.
- `get_active_positions()` always returns the same DataFrame schema, even when empty.
- `close_position()` calls the broker adapter and converts errors to typed results instead of bubbling raw exceptions.
- `submit_order()` is an **adapter onto the engine**, described below.

### Order submission: the engine seam

Manual orders do not reach the broker. `submit_order()` builds a real engine
`Executor` in-process and submits through it, so a hand-entered trade passes the
same gates as an automated one:

```
submit_order()
  ├─ load_engine_config()
  │    ├─ mode == LIVE  → Result(False, "LIVE_UI_DISABLED")   # refused here,
  │    │                                                       # before anything is built
  │    └─ mode == PAPER → continue
  ├─ RiskEngine.evaluate() → RiskApproval            (engine/risk.py)
  ├─ make_live_guard() → LiveGuard                   (engine/guard.py)
  ├─ build_executor(mode, journal, state, tick_provider, live_guard)
  └─ executor.prepare_order() → executor.submit(request, approval)
                                                     (engine/executor.py)
```

Gate order inside `submit` is `_check_sl` → `_check_risk` → `_check_state` →
`_check_invalidation`. An `ExecutionRejected` returns as
`Result(ok=False, code="EXECUTION_REJECTED", message=str(exc))` — the engine's own
sentence, unparaphrased, so the frontend banner reports the reason the backend
actually gave rather than a UI-authored summary of it.

**LIVE is refused from the UI by design.** Constructing a `LiveExecutor` here
would build a `Gateway` and open a second MT5 session inside Streamlit, competing
with the one `data_engine` already owns. Live trading stays CLI-only; the UI obeys
`config.yaml` for PAPER and refuses LIVE outright.

**Absolute prices.** `OrderRequest.sl`/`tp` are absolute prices, so the adapter
converts the caller's pip-ish units with the ×10 point convention inherited from
the legacy path: `round(price ∓ sl * point * 10, digits)`. Changing that
convention would silently rescale every stop the UI sends, so it is preserved
rather than modernised.

**The anchor is re-checked at the dispatch instant.** `engine/guard.py` builds a
`LiveGuard` from caller-supplied callables, because the engine layer must not
import `data_engine`. The closure re-reads the live price and tests the structural
leg's 100% invalidation line immediately before dispatch. It fails **closed**:
absent candles, an unreadable price, or a raising provider all block the order — a
check that cannot run is not a check that passed. An absent *leg*, by contrast,
passes: `select_structural_leg` is strict, so blocking on absence would reject
nearly every manual order for being unevaluable rather than for being broken.

The AI Signals tab passes the leg its intent was actually built on
(`leg=dry_run.signal.leg`); the manual path derives one from a fresh candle
snapshot. Re-deriving on the AI path would test a *different* structure and defeat
the point of handing one in.

**Account equity.** The risk-percentage gates are evaluated against **live broker
equity**, with the journal supplying only `start_of_day_equity` and `peak_equity`.
`engine/dryrun_cli.py` uses `baselines.peak_equity`; a percentage floor measured
against a stale peak is not a floor on current capital.

**Known wart.** `LiveGuard.price_provider` is declared but never read by
`verify()`, so the `invalidation_check` closure captures the price source itself.
Documented rather than fixed; changing it would touch the guard's signature.

## Risk guardrails

`ui/risk_calc.py` keeps the computations pure and importable without Streamlit or MT5. It handles lot normalization, tick-size conversions, and stop-risk calculations with explicit safeguards for invalid data.

## News feed

`ui/services/news.py` fetches the public ForexFactory XML feed, parses it with defused XML parsing, and falls back to cached data when the network is unavailable. It never raises on malformed XML and keeps the feed read-only.

## Repository hygiene

Three runtime artifacts are excluded in `.gitignore` because they are regenerated
on every run: committing them adds churn, and a tracked cache is worse than an
untracked one because it silently diverges from the machine that produced it.

| Pattern | Why |
|---|---|
| `*.db-wal` | SQLite write-ahead log — memory-mapped companion to `data/engine.db`, rewritten on every transaction commit. |
| `*.db-shm` | SQLite shared-memory index for the WAL. Same lifetime as the log. |
| `data/news_cache.json` | Transient ForexFactory cache written by `ui/services/news.py`; regenerated on the next fetch. |

`*.db` (line 21) does **not** cover the first two: a suffix pattern matches a
complete final extension only, so `engine.db-wal` is not a `*.db`. These barriers
are load-bearing, not decorative.

Verify with:

```
git check-ignore -v data/engine.db-wal data/engine.db-shm data/news_cache.json
```

## Architectural invariants

Enforced by `tests/test_engine/test_boundaries.py` on the AST rather than by
review, because a single stray import reintroduces the bug and no functional test
would notice:

- `engine/gateway.py` is the only engine module that may import `MetaTrader5`.
- No module outside `engine/keys.py` may read `os.environ` for a secret.
- No module under `ui/` may call `order_send`, `send_order`, `order_check`, or
  `execute_trade`.

The scans parse the AST rather than matching text: `ui/data_access.py` names
`execution.execute_trade` inside a docstring whose whole purpose is to deny it, so
a substring match would report the module's own documentation as the violation it
disclaims.

## Design system v2

Tokens live only in `ui/theme.py` (`TOKENS`). `.streamlit/config.toml` mirrors the shell colours so the first paint is dark. Do not hardcode a new hex in a component. Add the colour to `TOKENS`, expose it as a CSS variable inside `_css()`, and read `TOKENS[...]` from Python.

Accent tokens: `accent_cyan` (market and data), `accent_green` (account and positive), `accent_amber` (warnings and news), `accent_violet` (AI and agents), `accent_red` (risk and negative). Direction still uses ▲, ▼, or a text label. Colour is never the only signal.

### How to add a card

```python
from ui.components.cards import accent_card

with accent_card("cyan", "market"):
    st.subheader("Market")
```

`accent_card` opens `st.container(border=True, key="card-<accent>-<name>")`. Streamlit 1.64 turns that key into the class `st-key-card-<accent>-<name>`. The theme CSS matches `[class*="st-key-card-cyan"]` (and the other four accents): 1px accent border at about 70% alpha, a 18px glow at about 25% alpha, 14px radius, a faint line grid, and a hover glow only when the user has not asked for reduced motion.

The key must stay unique on the page. A second card with the same accent and name replaces the first.

### Glass background

`inject_theme()` paints one fixed layer, class `glass-bg`, behind the page. It is a deep gradient plus five blurred orbs. The orbs animate with `transform` only, on different 48–84s loops. The layer is `pointer-events: none` and sits under the content. `prefers-reduced-motion: reduce` freezes the orbs. Cards that support `backdrop-filter` blur 10px over `panel_glass`; everyone else gets the opaque panel colour.

### Navigation

`ui/components/nav.py` draws the five views as buttons plus a click `st.popover` (the ▾). Streamlit has no hover menu. The popover reads the `RunSnapshot` it was given. It does not fetch. "Open <view>" sets `active_tab` through the same mirror-key path as the nav button (`w_active_tab`, then `persist_active_tab()`). Only the active view is built.

### One snapshot

`ui/components/snapshot.py` `load_shared_snapshot(refresh=True)` fetches the account, positions, journal, news, and quote once and stores that object. Later calls in the same run use `refresh=False` and return the same instance. The Portfolio view is drawn inside the header fragment so a 2s tick cannot show a different equity than the header.

### Tables and news HTML

Positions, Portfolio exposure, and Journal use `render_html_table` / `render_journal_table`. Every cell goes through `html.escape`. The table sits in `.table-scroll` so a narrow column can scroll sideways, and header cells are bold. Journal times are relative ("2h ago") with the absolute IST stamp in the `title` attribute.

Live Market News is one HTML block from `build_news_html`. The grid is `repeat(auto-fill, minmax(260px, 1fr))`. Feed strings are escaped. High-impact cards are expanded, medium cards are compact, and low-impact plus holidays sit in one collapsed group. Filters reuse the snapshot feed.

## Task 5

`docs/Instructions_2.md` was not edited.

Phases B (screen defects) and E (motion) stay **PARTIAL** until a person signs off in the browser. Popovers are click controls. Streamlit does not open them on hover.

### Indicator reuse

`engine/indicators` has structure tools and one `ema_ribbon` snapshot. It does not provide RSI, MACD, ATR, or Bollinger series, and `ema_ribbon` refuses a short sample. The chart series live in `ui/indicators.py`. A value at index `i` uses only rows `0..i`. Signals stop before the forming bar (`upto = len - 1`). The caption on the chart says so.

Signal rules, closed bars only:

- EMA: fast crosses above slow = buy, below = sell.
- RSI: leaving below 30 = buy, leaving above 70 = sell.
- MACD: line crosses its signal the same way.
- Bollinger: close re-enters the band after a break. A lower break then re-entry is buy. An upper break then re-entry is sell.
- ATR is not a direction. Stop distance is `atr_k × ATR` (default 1.5). Take-profit distance is `rr` times that stop (default 3.0, range 1–10) in `config_ext.yaml`. Prices round to the symbol tick. A stop inside `stops_level` is rejected and the reason is shown.
- The latest signal draws dashed SL and TP lines. "Use in order form" copies side and stop only. It does not submit. The market order path can send a take-profit. This button does not, and the chart says so.

The break-even sentence on the chart is exactly: "Break-even win rate at 1:3 is 25% before spread/commission."

### Alerts

`ui/alerts.py` uses warn level `100.0`. `config.yaml` has no separate margin-level percent. `margin_level_na_below: 1.0` is only a display cutoff. The open-risk cap is the engine setting `risk.max_total_exposure_pct` (10). One banner sits under the header. Dismiss hides that signature for the session and does not block the page.

### Nav contrast

Text `#E6E8EE` on the inactive glass mix `#121821` is 14.55:1. On the active 22% accent tint it is 7.97:1 (Exchange), 8.52:1 (Portfolio), 8.63:1 (AI Signals), 8.57:1 (News), and 7.98:1 (Journal `#5EEAD4`). All are above 4.5:1.

### Theme Studio and MutationMechanic

MutationMechanic's picker is not a hex picker. `THEMES` is a 10-item array in `E:\MutationMechanic\App.tsx`. Settings buttons show a white check. The swatch classes do not match the hex that gets applied (for example the purple swatch is `#6366f1` while the preset writes `#C5B4E9`). `applyTheme` sets only `--color-primary` and `--color-primary-hover`, and stores the id in `localStorage` under `mutationMechanic_theme`. With no saved id the check sits on purple while the page stays teal `#22D3EE`. Red and pink share the same primary. An unknown id is ignored. Related dim, glow, and ring tokens stay put, and some hovers stay hardcoded teal. White text on a pastel fails contrast.

Keep named preset ids, store the id, keep the dark surfaces, and keep a named swatch plus a selected marker and an accessible name. Improve by previewing the real colour, deriving the related tokens, and rejecting pairs that fail contrast. Do not copy that app's hex values into this palette.

This app's Theme Studio is `ui/components/theme_studio.py`, opened from the header popover `🎨 Theme Studio`. Presets are Aurora Cyan, Emerald Ledger, Amber Terminal, Violet Nebula, Crimson Edge, and Monochrome Glass. Gain and loss stay green and red in the monochrome preset. The palette is checked on load (`extra=forbid`, `#RRGGBB` only) and saved to gitignored `data/ui_prefs.json`. A failing tint has its lightness moved to the nearest passing value. Gain and loss hues closer than 40° produce a warning. ▲ and ▼ stay. Glow and motion are off, low, or normal. Reset, export, and import ship the palette JSON only. No secrets. Hex literals stay in `ui/theme.py`.

### Equity curve

The cleaner drops equity at or below 0 and non-finite values, and it keeps only the latest segment when a gap is over 30 minutes. A point outside half to double the segment median is a cliff and is left out when at least two points remain. The caption is a plain sentence. It does not print the raw series. On this database a single positive point still draws —. The area fill, when there are two points, starts at the data minimum rather than at zero. The y axis is padded from that same range.

## Task 7

`docs/Instructions_2.md` section 10. `docs/Instructions*.md` was not edited.

### Defects

- The risk card is green only when `risk_card_accent` is given no codes. `TOTAL_RISK_EXCEEDED`, margin, stop, and SL codes are red. Any other shown code is amber. The success sentence is not used while a code is showing.
- `quote_clock.broker_tick_age_seconds` corrects a broker server clock that is a whole number of hours ahead of UTC. A fresh UTC+3 tick is a few seconds old, not about −10800s. IST (UTC+5:30) is not snapped onto an hour. The age passed to the header is never negative. `data_engine.get_symbol_live_quote` uses that age for the stale check too.
- RSI, ATR, and MACD chips use `indicator_chip_text` on the last closed bar and the symbol's digits. "—" means the series is not selected or the closed bar is still warming up.
- The margin donut centre uses one Plotly size that fits the 390px column as well as 1280 and 1920. Plotly does not resize the annotation with the browser.
- The FOMC countdown sits with the quote. Theme Studio sits in the clock column, so its popover is not the empty band above the chip. `.news-countdown` has `margin-top: 0`.
- Header and Portfolio still share one `RunSnapshot` per run. `tests/test_ui/test_snapshot_identity.py` fetches the account and the positions once.

### Research data

Infoway's official HTTP host is `https://data.infoway.io`. The key is the `apiKey` header, not a query parameter. `ret` 200 is success. HTTP 200 with any other `ret` is a failure. Forex and metals use `/common/batch_trade/{codes}` and `POST /common/v2/batch_kline`. The published free REST plan is 1 request per second, 60 per minute, and 86,400 per day (docs page dated 2026-09-21). Terms at `https://infoway.io/en/terms-and-conditions` allow personal, non-commercial analysis and research. They do not allow redistribution. This dashboard is that personal research display. Infoway is not the price used for orders.

`ui/services/infoway_client.py` is read-only. It uses a strict Pydantic envelope, a 5 second timeout, no retry on timeout, a 30 second cache, and `infoway.daily_cap` from `config_ext.yaml` (200). The count is `data/infoway_budget.json` (date and count only, gitignored). Any error, including a schema mismatch, returns status `degraded`. The Exchange button "Show Infoway quote" is outside the 2 second header fragment. Tests use `tests/fixtures/infoway/` and do not open a socket. `INFOWAY_API_KEY` does not stand in for `FINANCIAL_DATASETS_API_KEY`.

Finviz is link-out only: `https://finviz.com/forex.ashx`, `https://finviz.com/futures.ashx`, and `https://finviz.com/quote.ashx?t=SYMBOL` for a letter ticker of at most five characters that is not forex, metals, crypto, or an index. Nothing is scraped. An official Elite export could be integrated if the operator subscribes.

Anchor Browser (`https://docs.anchorbrowser.io/introduction`) is a cloud browser for agents: navigate, authenticate, fill forms, and extract data, then dispose of the VM. The vendor examples use `ANCHOR_API_KEY`. This repo's placeholder name, from the task, is `ANCHORBROWSER_API_KEY`. No client, SDK, or dependency was added. It is not needed yet. This UI does not automate third-party websites.

The Ideas watchlist is typed by hand: source, symbol, direction (`long`, `short`, or `watch`), and a note. It is stored in gitignored `data/ideas_watchlist.json`. The table is escaped. It does not submit orders and it does not read agent scores.

### Task 6

Section 9 is not in `docs/Instructions_2.md`. The file begins at section 10. Dexter was not installed and the Research Desk was not built.
