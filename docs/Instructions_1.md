# CODER AGENT BRIEF — EchoFi-style Dashboard Redesign for HedgeFund_Binod_v2

You are a senior Python/Streamlit engineer with full access to this repository. Implement the redesign below **in the existing codebase**. Read the whole brief before touching any file.

Attached references (also saved under `docs/design/`, view them before starting):
- `current_dashboard.png` — the CURRENT app. Preserve every capability visible here (AI Brain Reactor, SCAN & EXECUTE, instrument radar, indicator chips, risk matrix, account strip).
- `echofi_reference.png` — the VISUAL TARGET (layout rhythm, card styling, typography, chart treatment). Match its look, not its crypto-specific content.

---

## 0. NON-NEGOTIABLES

1. **Broker/engine is the source of truth.** This is a live MT5 trading terminal. Do NOT create a second `positions` table, a simulated portfolio, or fake default equity (e.g. 100000). Positions, balance, equity, margin and P&L come from the existing telemetry/journal/gateway layers (`data_engine.py`, `execution.py`, `engine/journal.py`, `engine/gateway.py`).
2. **No order bypasses the risk/safety path.** The execution form must go through the existing execution/command pipeline (`execution.py` and/or `engine/commands.py` → `engine/executor.py` → `engine/risk.py`). The UI never writes an order straight to a database or calls `mt5.order_send` itself.
3. **Do not break existing behavior.** `pytest` must be green before you start (record the baseline) and after every phase. Do not edit `.env`, `config.yaml` semantics, or anything in `engine/` except where a phase explicitly says so.
4. **No secrets in code, logs, screenshots or tests.** Account numbers stay masked via `formatting.py`.
5. **Fail closed.** If data is stale, missing, or the engine is halted/offline, the execution form is disabled with a visible reason. Never show a default number as if it were live.

---

## 1. RECONCILIATION WITH THE ORIGINAL BLUEPRINT (decisions already made)

| Original blueprint item | Decision |
|---|---|
| SQLite `trading_dashboard.db` with own `positions` table + `add_position/close_position` | **Replaced.** Create a thin data-access module `ui/data_access.py` exposing `get_active_positions() -> DataFrame`, `get_account_snapshot() -> AccountSnapshot`, `submit_order(...)`, `close_position(ticket)`. Internally these call the existing layers. SQLAlchemy is only allowed for **read-only** access to the engine journal if `engine/journal.py` already uses a SQL DB (inspect it first); never a parallel store. |
| `st.session_state.portfolio` dict with 100000 equity defaults | **Replaced** by a read-only `AccountSnapshot` refreshed from the broker each cycle. Defaults are `None`/`connected=False`. |
| Tabs: Exchange, Portfolio, NFTs, AI Signals, Live Market News | **NFTs removed** (irrelevant to FX/MT5). Final tabs: `🌐 Exchange`, `💼 Portfolio`, `🤖 AI Signals`, `📰 Live Market News`, `🧾 Journal` (journal = read-only view of engine events; optional, build last). |
| Asset categories Crypto/Forex/Stocks | Categories and instruments come **only** from the `config.yaml` whitelist via `config_loader.py`. Show only categories that have ≥1 whitelisted symbol. |
| Order types Market/Limit/Conditional | Show all three, but enable only what `execution.py` actually supports. Unsupported ones are rendered disabled with a tooltip "Not supported by engine yet". Do not fake them. |
| ForexFactory RSS at `https://forexfactory.com` | **That URL is the HTML homepage, not a feed.** Use the public calendar XML feed `https://nfs.faireconomy.media/ff_calendar_thisweek.xml` (verify it responds before coding against it; see §9). |
| "Write the entire app.py" | `app.py` stays a thin orchestrator (<~250 lines). New code lives in a `ui/` package (§3). |
| Layout: current 3 columns | Exchange tab becomes 70/30 as specified. The **AI Brain Reactor** (HOLD ring, signal pill, target/age/R:R, rationale, SCAN & EXECUTE) moves intact to the `🤖 AI Signals` tab, plus a compact signal badge in the account strip. |

---

## 2. EXECUTION PHASES AND CHECKPOINTS

Stop and report at each checkpoint. Do not start the next phase until the gate passes.

**Phase A — Audit (read-only, no edits).**
Read `app.py`, `data_engine.py`, `execution.py`, `metrics.py`, `formatting.py`, `config_loader.py`, `engine/commands.py`, `engine/journal.py`, `engine/risk.py`, `engine/state.py`, `requirements.txt`, `.streamlit/config.toml` (if any).
Deliver: (1) the Streamlit version pinned and which features you may use (`st.fragment` ≥1.37, `st.dialog` ≥1.34, `st.segmented_control` ≥1.40, `st.tabs(on_change=…)` only in very recent versions); (2) how orders and closes currently flow; (3) how telemetry/heartbeat age is exposed; (4) which of Market/Limit/Conditional are supported; (5) a list of every current widget key in `app.py` (to avoid collisions); (6) pytest baseline result.
**Gate A:** written findings + green baseline.

**Phase B — Theme layer.** Add `ui/theme.py` (tokens + CSS injection, §5) and `.streamlit/config.toml` theme. Restyle only; no logic changes.
**Gate B:** app runs, all existing features still work, screenshot compared against `echofi_reference.png`.

**Phase C — State + data access.** Add `ui/state.py` (§4) and `ui/data_access.py` (§6). Wire existing app to them without changing the visible layout yet.
**Gate C:** unit tests for state init and data-access error paths pass.

**Phase D — Risk math module.** `ui/risk_calc.py` as **pure functions** (§7) with tests.
**Gate D:** all risk tests pass, including the worked examples in §7.

**Phase E — Layout.** Header/account strip, tab navigation, Exchange 70/30 grid, analytics row, execution form, risk panel, positions matrix, AI Signals tab.
**Gate E:** manual checklist in §11 passes; no flicker (§6.3).

**Phase F — News tab.** `ui/services/news.py` (§9) with fixture-based tests.
**Gate F:** malformed-input tests pass; tab degrades gracefully offline.

**Phase G — Hardening + docs.** Edge-case matrix (§10), update `docs/` (architecture + a short `docs/ui.md`), remove dead code.
**Gate G:** full `pytest` green, `scripts/secret_scan.py` clean, definition of done (§12).

---

## 3. FILE LAYOUT

```
app.py                      # set_page_config, init_state, inject_theme, render shell only
ui/
  __init__.py
  theme.py                  # TOKENS dict + inject_theme() + plotly template
  state.py                  # DEFAULTS, init_state(), persist helpers, callbacks
  data_access.py            # AccountSnapshot, get_active_positions, submit_order, close_position
  risk_calc.py              # pure functions, no streamlit/mt5 imports
  components/
    header.py               # nav + account strip
    chart.py                # plotly figure builders
    analytics_row.py
    order_form.py
    positions_matrix.py
    ai_signals.py           # existing Brain Reactor, moved
    news_tab.py
  services/
    news.py                 # fetch + parse, no streamlit import except cache wrapper
tests/test_ui/
  test_risk_calc.py
  test_state.py
  test_news_parser.py       # with fixtures in tests/test_ui/fixtures/
```

`risk_calc.py` and `services/news.py` must be importable and testable without a running Streamlit or MT5.

---

## 4. STATE ARCHITECTURE

Initialize **once, at the top of `app.py`, before any widget**, with `setdefault` (never overwrite):

```python
# ui/state.py
import copy
import streamlit as st

DEFAULTS = {
    "active_tab": "🌐 Exchange",
    "asset_category": "Forex",          # must exist in whitelist, validated below
    "selected_asset": None,             # resolved from whitelist at init (first symbol); NOT hardcoded "EUR/USD"
    "chart_style": "Candlestick",       # Candlestick | Area | Bar
    "timeframe": "1H",                  # 15M | 1H | 4H | 1D | 1Y | All
    "order_side": "BUY",
    "order_type": "Market",
    "exposure_pct": 0,                  # 0..100
    "slippage_pips": 1.0,
    "lot_size": 0.01,
    "sl_pips": 30.0,
    "pending_close_ticket": None,       # ticket awaiting confirmation
    "last_order_token": None,          # idempotency guard
    "last_good_news": None,             # (fetched_at, list[dict])
    "account": None,                    # AccountSnapshot | None
}

def init_state(whitelist: dict[str, list[str]]) -> None:
    for k, v in DEFAULTS.items():
        st.session_state.setdefault(k, copy.deepcopy(v))
    # validate persisted values against current config (config may have changed between sessions)
    cats = [c for c, syms in whitelist.items() if syms]
    if st.session_state.asset_category not in cats:
        st.session_state.asset_category = cats[0]
    syms = whitelist[st.session_state.asset_category]
    if st.session_state.selected_asset not in syms:
        st.session_state.selected_asset = syms[0]
```

**Rules that prevent the classic Streamlit state bugs:**
1. **Widget state is dropped when a widget is not rendered in a run** (e.g. when its tab/view isn't active). For every user-facing choice (timeframe, chart style, lot size, SL, side, order type) keep a **persistent mirror key** (`timeframe`) separate from the widget key (`w_timeframe`). Pattern: widget `key="w_timeframe"`, `on_change` callback copies into `timeframe`; widget `index/value` is read from the persistent key.
2. Never assign to `st.session_state[widget_key]` after that widget has been instantiated in the same run (raises). Mutate only inside callbacks.
3. All widget keys are namespaced: `w_*` for widgets, `btn_*` for buttons, `close_{ticket}` for per-row buttons.
4. **Tab binding:** `st.tabs` renders every tab body on every rerun and does not reliably expose the active tab. Preferred: `st.segmented_control` (or horizontal `st.radio`) with `key="w_active_tab"` styled to look like tabs, and render **only the active view**. This gives true persistence and avoids running the news fetch/chart code in hidden tabs. If the pinned Streamlit is too old for either, fall back to `st.tabs` and make every tab body lazy (cheap or cached).
5. Selecting a new category must reset `selected_asset` to a valid member in the `on_change` callback.

---

## 5. DESIGN SYSTEM (EchoFi DNA) AND CSS INJECTION

### 5.1 Tokens (single source of truth, `ui/theme.py`)

```python
TOKENS = {
    "bg": "#121318",          # canvas
    "panel": "#1A1C24",       # cards
    "panel_hover": "#20232D",
    "border": "#262A36",
    "text": "#E6E8EE",
    "text_muted": "#8B90A0",
    "gain": "#00E676",
    "loss": "#FF3B30",
    "warn": "#FFB020",
    "accent": "#B8F2FF",      # EchoFi pale-cyan highlight (selected bar, gauge)
    "radius": "12px",
    "font_ui": "'Manrope','Inter',system-ui,sans-serif",
    "font_mono": "'JetBrains Mono','IBM Plex Mono',ui-monospace,monospace",
}
```
Use the same tokens for the Plotly template so charts and CSS never drift. Mirror bg/panel/text/primary in `.streamlit/config.toml` `[theme]` so there is no white flash before CSS loads.

### 5.2 CSS strategy (rules)
- Inject **once per run** via `st.markdown("<style>…</style>", unsafe_allow_html=True)` at the very top of `app.py` (right after `set_page_config`). Use CSS variables on `:root` generated from `TOKENS`.
- Target **stable hooks only**: `.stApp`, `[data-testid="stAppViewContainer"]`, `[data-testid="stHeader"]`, `[data-testid="stMetric"]`, `[data-testid="stMetricValue"]`, `[data-testid="stMetricDelta"]`, `[data-testid="stTabs"]`/`button[role="tab"]`, `[data-testid="stVerticalBlockBorderWrapper"]` (bordered containers), `[data-testid="stNumberInput"]`, `[data-testid="stSelectbox"]`, `[data-testid="stSlider"]`, `[data-testid="stDataFrame"]`, `.stButton > button`.
- **Never** target emotion-generated classes (`.css-1xyz…`, `.st-emotion-cache-*`); they change every Streamlit release.
- Cards are built with `st.container(border=True)` and styled through the bordered-container selector; do not hand-roll `<div>` wrappers around Streamlit widgets (HTML you open in one `st.markdown` cannot wrap widgets in a later call).
- Use `!important` sparingly and only for Streamlit-inline overrides.
- After a Streamlit upgrade, a visual smoke test (§11) is mandatory because `data-testid` values can change.

### 5.3 Baseline CSS (adapt, don't paste blindly; verify each selector against the pinned version)

```css
:root{
  --bg:#121318; --panel:#1A1C24; --panel-hover:#20232D; --border:#262A36;
  --text:#E6E8EE; --muted:#8B90A0; --gain:#00E676; --loss:#FF3B30; --warn:#FFB020;
  --accent:#B8F2FF; --r:12px;
  --font-ui:'Manrope','Inter',system-ui,sans-serif;
  --font-mono:'JetBrains Mono','IBM Plex Mono',ui-monospace,monospace;
}
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700&family=JetBrains+Mono:wght@400;600&display=swap');

.stApp, [data-testid="stAppViewContainer"]{ background:var(--bg); color:var(--text); font-family:var(--font-ui); }
[data-testid="stHeader"]{ background:transparent; }
.block-container{ padding:1rem 1.5rem 3rem; max-width:100%; }
#MainMenu, footer{ visibility:hidden; }

/* cards */
[data-testid="stVerticalBlockBorderWrapper"]{
  background:var(--panel); border:1px solid var(--border)!important; border-radius:var(--r); padding:.25rem;
}
/* metrics */
[data-testid="stMetric"]{
  background:var(--panel); border:1px solid var(--border); border-radius:var(--r); padding:.75rem 1rem;
}
[data-testid="stMetricLabel"] p{ color:var(--muted); font-size:.72rem; letter-spacing:.06em; text-transform:uppercase; }
[data-testid="stMetricValue"]{ font-family:var(--font-mono); font-variant-numeric:tabular-nums; }

/* numerics everywhere that shows prices */
.mono, [data-testid="stNumberInput"] input, [data-testid="stDataFrame"]{ font-family:var(--font-mono); font-variant-numeric:tabular-nums; }
.pos{ color:var(--gain); } .neg{ color:var(--loss); }

/* tabs / segmented nav (sticky) */
[data-testid="stTabs"] [role="tablist"], [data-testid="stSegmentedControl"]{
  position:sticky; top:0; z-index:50; background:var(--bg); border-bottom:1px solid var(--border);
}
button[role="tab"]{ color:var(--muted); }
button[role="tab"][aria-selected="true"]{ color:var(--text); border-bottom:2px solid var(--accent); }

/* inputs */
[data-testid="stNumberInput"] > div > div, [data-baseweb="select"] > div{
  background:var(--panel-hover); border:1px solid var(--border); border-radius:10px;
}
/* primary action = EchoFi white pill */
.stButton > button[kind="primary"]{
  background:#fff; color:#121318; border:0; border-radius:10px; font-weight:700;
}
.stButton > button[kind="primary"]:disabled{ background:#3a3d49; color:#8B90A0; }
.stButton > button[kind="secondary"]{
  background:transparent; color:var(--text); border:1px solid var(--border); border-radius:10px;
}
/* BUY / SELL accent on the segmented toggle (selected state) */
.side-buy [aria-checked="true"]{ color:var(--gain); } .side-sell [aria-checked="true"]{ color:var(--loss); }
```
Add: scrollbar styling, plotly transparent paper bg, `@media (max-width:900px)` stacking, `prefers-reduced-motion` guard for any transitions.

### 5.4 Plotly template
`paper_bgcolor/plot_bgcolor` transparent, grid `#262A36` at 40% opacity, font = mono for axes, increasing = gain, decreasing = loss, `hovermode="x unified"`, `margin=dict(l=8,r=8,t=8,b=8)`, `uirevision` set (see §6.3), `config={"displayModeBar": False}`. Keep the existing EMA20/EMA50 overlays and indicator chips (RSI, ATR, EMA, MACD) from the current app.

### 5.5 Dev-only chrome
In the current screenshot the Streamlit "File change / Rerun / Always rerun" toolbar overlaps the Margin Level metric. Set `[client] toolbarMode = "minimal"` (or `"viewer"`) in `.streamlit/config.toml`, and add top padding so no header element overlaps the account strip.

---

## 6. DATA FLOW (SQLAlchemy/journal ↔ Streamlit) AND ANTI-FLICKER

### 6.1 Data-access contract (`ui/data_access.py`)

```python
@dataclass(frozen=True)
class AccountSnapshot:
    connected: bool
    mode: str                 # "LIVE" | "PAPER"
    engine_state: str         # from engine/state.py: running | paused | halted | offline ...
    heartbeat_age_s: float | None
    currency: str
    balance: float | None
    equity: float | None
    margin_used: float | None
    free_margin: float | None
    margin_level: float | None   # None when margin_used == 0
    open_pnl: float | None
    open_risk: float | None
    fetched_at: datetime         # tz-aware UTC
```
- Every function catches broker/DB exceptions and returns a **typed degraded result** (`connected=False`, empty DataFrame with the correct columns) plus logs the error. The UI never receives a raw exception.
- `get_active_positions()` returns a DataFrame with fixed columns: `ticket, symbol, side, lots, entry, current, sl, tp, pnl, risk_usd, opened_at`. Fixed schema even when empty (prevents `KeyError` and layout jumps).
- Any DB session is opened with a context manager per call (`with Session() as s:`), read-only, short-lived. Never store a Session or Connection in `st.session_state`.
- `submit_order()` and `close_position()` return `Result(ok: bool, code: str, message: str, ticket: int | None)`. They call the existing pipeline; the engine's risk gate has the final say, and its rejection reason is surfaced verbatim.

### 6.2 One snapshot per run
At the top of each rerun (and each fragment tick) call `get_account_snapshot()` and `get_active_positions()` **once**, store in locals, and pass them down to every component. Components never fetch independently. This keeps every number on screen consistent with the same instant.
Wrap with `@st.cache_data(ttl=1, show_spinner=False)` keyed by nothing but a time bucket so multiple fragments in the same second share one broker call.

### 6.3 Flicker prevention
- Live regions (price ticker, account strip, positions matrix) are `@st.fragment(run_every="2s")` so only they refresh. The chart fragment refreshes every 5s, or on user input. **No `time.sleep` loops, no `st.rerun()` polling, no `st.empty()` redraw loops.**
- Fixed heights for chart and matrix containers so a refresh never changes layout height. Use skeleton text (`—`) for missing values rather than removing elements.
- Plotly: stable `key="chart_main"`, `fig.update_layout(uirevision=f"{symbol}-{timeframe}")` so zoom/pan survive refreshes; changing symbol/timeframe changes the revision on purpose.
- Fragments must not mutate widget keys owned by other fragments. Use `st.rerun(scope="app")` only after a confirmed order/close to refresh everything once.
- If `heartbeat_age_s` exceeds the staleness threshold (take it from engine config; default 15s), show a "STALE DATA" badge and disable execution.

---

## 7. MATHEMATICAL RISK GUARDRAILS (`ui/risk_calc.py`, pure and unit-tested)

Inputs come from MT5 `symbol_info` via the data layer: `digits, point, trade_tick_size, trade_tick_value_loss (account currency per tick per 1.0 lot), trade_contract_size, volume_min, volume_max, volume_step, trade_stops_level`.

```python
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

@dataclass(frozen=True)
class SymbolSpec:
    symbol: str; category: str          # forex | metals | indices | crypto | stocks
    digits: int; point: float
    tick_size: float; tick_value_loss: float; contract_size: float
    volume_min: float; volume_max: float; volume_step: float
    stops_level_points: int
```

**Unit conversion.**
- Forex: `pip_size = point * 10` if `digits in (3, 5)` else `point`.
- Non-forex (metals, indices, crypto, stocks): the "pip" concept is ambiguous, so the SL input unit is **points** (`pip_size = point`) and the label switches to "Stop Loss (points)". Never silently reuse the forex pip.

**Stop distance:** `sl_distance_price = sl_units * pip_size`

**Open Risk (account currency):**

```
ticks_at_risk = sl_distance_price / tick_size
open_risk     = lots * ticks_at_risk * tick_value_loss
risk_pct      = open_risk / equity * 100
```
Label uses the real account currency (`USD` in your screenshot), not a hardcoded "USD".

*Worked examples (use as tests):*
- EURUSD, 1 lot, 30 pips: pip 0.0001, tick 0.00001, tick value $1 → ticks = 0.0030/0.00001 = 300 → risk = 1×300×$1 = **$300**. At 0.08 lot → **$24**.
- Quote ≠ account currency (e.g. USDCAD, account USD): trust MT5's `tick_value_loss` (already converted to account currency). Do **not** assume `0.0001 × 100000`. Add a cross-check in the integration layer: `mt5.order_calc_profit(ORDER_TYPE_BUY, symbol, lots, price, price - sl_distance)`; if `abs(calc) ` differs from the formula by >2%, log a warning and **prefer the broker value**.

**Total portfolio open risk:** `Σ risk_usd(position)` computed from each position's current SL (if SL = 0 the position's risk is "unbounded" → flag, count as `inf` for the warning, never as 0).

**Volume normalization (always before any check or submit):**

```python
def normalize_lots(raw: float, spec: SymbolSpec) -> float:
    step = Decimal(str(spec.volume_step))
    lots = (Decimal(str(raw)) / step).to_integral_value(rounding=ROUND_DOWN) * step
    lots = max(Decimal(str(spec.volume_min)), min(lots, Decimal(str(spec.volume_max))))
    return float(lots)
```

**Margin check:** `required = mt5.order_calc_margin(action, symbol, lots, price)` (via data layer). If it returns `None`, treat as a hard failure (cannot verify → block).
```
projected_free_margin = free_margin - required
projected_margin_level = equity / (margin_used + required) * 100
```
Block when `required > free_margin * (1 - margin_buffer)`; warn when `projected_margin_level < warn_level`. `margin_buffer` and warn level come from `config.yaml` (add keys with strict validation in `config_loader.py` only if absent; otherwise reuse existing ones).

**Exposure slider → lots:** `lots = normalize_lots((exposure_pct/100 * free_margin) / margin_per_lot, spec)` where `margin_per_lot = order_calc_margin(…, 1.0, …)`. Slider at 0% → `volume_min` is NOT auto-selected; show "enter lot size".

**Pre-submit validation (all must pass, each returns a machine-readable code and a human message):**
1. `ENGINE_NOT_READY` (halted/offline/stale/paused)
2. `SYMBOL_NOT_WHITELISTED`
3. `MARKET_CLOSED` / no fresh tick
4. `LOTS_INVALID` after normalization (0, below min, NaN)
5. `SL_REQUIRED` (house rule: no order without a stop) and `SL_TOO_CLOSE` (< `stops_level_points` incl. spread)
6. `RISK_PER_TRADE_EXCEEDED` (risk_pct > config max per trade)
7. `TOTAL_RISK_EXCEEDED` (portfolio open risk + this trade > config cap)
8. `MARGIN_INSUFFICIENT` (see above)
9. `SPREAD_TOO_WIDE` (vs config)
10. `SLIPPAGE_INVALID` (negative / above config max)
11. `DUPLICATE_SUBMIT` (idempotency token reused)

**Live warning calculator** (below the lot/SL inputs): recomputes on every input change, shows `Open Risk (cur)`, `% of equity`, `Required margin`, `Projected margin level`; colors: ≤1% gain-green neutral, 1–2% amber, above the cap loss-red + Submit disabled. Guard all divisions (`equity <= 0`, `tick_size == 0`, `margin_used + required == 0`) → show "N/A", never raise.

---

## 8. EXCHANGE TAB LAYOUT (70/30)

**Header + account strip** (always visible): brand, view navigation (§4.4), LIVE/PAPER mode badge, engine-state badge, connection pill, masked account, session clock (IST). Strip metrics: Balance, Floating Equity, Floating P&L, Margin Used, Free Margin, Margin Level, Open Risk (USD and %), Open Positions, Last Tick age, Spread. Category + instrument selectors, bid/ask ticker with 24h change badge (compute from D1 candles; if unavailable show "—").

**Left 70%:**
- Control row: style toggle (`Candlestick | Area | Bar`), timeframe toggle (`15M | 1H | 4H | 1D | 1Y | All`, **map to MT5 timeframes explicitly**, `1Y`/`All` use D1/W1 with bounded bar counts to avoid huge fetches), Sync button.
- Plotly chart with EMA overlays, volume subplot, fixed height. Indicator chip row (RSI14, ATR, EMA20, EMA50, MACD) kept.
- Analytics row, 3 equal cards: (1) tick-volume bar chart per period, EchoFi highlight color on the selected bar; (2) half-donut gauge of margin allocation (used / free / buffer), center label = margin level; (3) equity-curve line from the journal's equity history (`engine/equity.py` / journal), "—" if less than 2 points. Replace tokenomics/stablecoin wording with trading-relevant labels.

**Right 30%:**
- Execution card: BUY/SELL segmented toggle (accent per side), order-type select, exposure slider, slippage input, estimated spread cost + commission (from symbol info, else "—"), primary Submit button labeled with side + symbol + lots.
- Submit flow: validate (§7) → show a **confirmation dialog** (`st.dialog`) summarizing side, symbol, lots, SL, risk, margin → on confirm, generate idempotency token, call `submit_order`, show result toast with the engine's reason on rejection. The button is disabled while a submission is in flight.
- Risk card: Lot Size, Stop Loss, live warning calculator (§7).
- Positions matrix: high-density table (fixed schema from §6.1) with per-row **Close** button (`key=f"close_{ticket}"`). Close is two-step: first click sets `pending_close_ticket`, a confirm popover/dialog appears, second click calls `close_position(ticket)`. P&L colored with `.pos/.neg`. Empty state: "No active open positions". Optional "Close all" must be behind its own confirmation and must show the count.

**🤖 AI Signals tab:** existing Brain Reactor moved unchanged in behavior: ring with HOLD/BUY/SELL, signal pill, LLM-asserted line, Target/Age/R:R tiles, Portfolio Manager rationale, SCAN & EXECUTE. Keep the "LLM-asserted, not an execution gate" disclaimer. SCAN & EXECUTE must go through the same validations as manual orders.

**💼 Portfolio tab:** account snapshot table, exposure by symbol, realized/unrealized P&L, drawdown from `engine/equity.py`.

---

## 9. FOREXFACTORY NEWS PIPELINE (`ui/services/news.py`)

**Endpoint:** `https://nfs.faireconomy.media/ff_calendar_thisweek.xml` (weekly calendar feed). **Before coding, fetch it once and inspect the real field names, date/time format and timezone**, and encode what you observe as constants; do not assume. Expected `<event>` children: `title, country, date, time, impact, forecast, previous` (some feeds add `url`). The feed is rate-limited and returns 429 or an HTML challenge page if polled too often; the provider's terms apply, so use it for display only and keep polling modest.

**Fetch rules:**
- `requests.get(url, headers={"User-Agent": "...", "Accept": "application/xml,text/xml"}, timeout=(3.05, 10))`, max 2 retries with exponential backoff + jitter, only on timeouts/5xx/429 (honor `Retry-After`).
- Reject if status ≠ 200, if `Content-Type` is not xml-ish, if body > 2 MB, or if the body starts with `<!DOCTYPE html`/`<html` (Cloudflare/interstitial).
- Parse with `defusedxml.ElementTree` (add to `requirements.txt`) to block entity-expansion/XXE attacks, falling back to stdlib `xml.etree.ElementTree` **only if defusedxml is unavailable**, after stripping any `<!DOCTYPE`/`<!ENTITY` declarations.

**Parser skeleton (implement fully):**

```python
@dataclass(frozen=True)
class NewsEvent:
    currency: str; title: str; when_utc: datetime | None
    impact: str            # "High" | "Medium" | "Low" | "Holiday" | "Unknown"
    forecast: str; previous: str

def parse_calendar_xml(raw: bytes) -> tuple[list[NewsEvent], list[str]]:
    """Returns (events, warnings). Never raises."""
    warnings: list[str] = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        return [], [f"XML parse error: {e}"]
    events = []
    for i, node in enumerate(root.iter("event")):
        try:
            title = (node.findtext("title") or "").strip()
            cur   = (node.findtext("country") or "").strip().upper()
            if not title or not cur:
                warnings.append(f"event {i}: missing title/currency"); continue
            impact = normalize_impact(node.findtext("impact"))
            when   = parse_when(node.findtext("date"), node.findtext("time"))  # None for "All Day"/"Tentative"
            events.append(NewsEvent(cur, title, when, impact,
                                    (node.findtext("forecast") or "").strip(),
                                    (node.findtext("previous") or "").strip()))
        except Exception as e:                 # one bad node must not kill the feed
            warnings.append(f"event {i}: {e}")
    return events, warnings
```
`parse_when` handles `"All Day"`, `"Tentative"`, empty, `1:30pm` and `13:30` style times, and converts from the feed's source timezone (confirm it from observation) to UTC, then to IST for display (`zoneinfo`).

**Caching and degradation:** wrap the fetch in `@st.cache_data(ttl=300, show_spinner=False)`, but **cache the result object, not exceptions**: the cached function catches all errors and returns `FetchResult(events, fetched_at, status, warnings)`. On failure, return the last good result from `st.session_state.last_good_news` (and a small on-disk JSON cache under `data/`) with a "Showing cached data from HH:MM, source unreachable" banner. If there is no cache at all, show an empty-state card, never a traceback.

**Display:** filter bar (currency multi-select defaulting to the currencies of the selected symbol, impact filter), sorted by time, grouped by day. Impact badge: High = `#FF3B30` card accent + bold, Medium = `#FFB020`, Low = muted. Past events dimmed; next upcoming High-impact event shown as a countdown chip in the account strip ("USD NFP in 02:14:05"). Optionally warn in the execution form when a High-impact event for either currency of the selected pair is within N minutes (N from config).

---

## 10. EDGE-CASE MATRIX (each needs a handled path and a test or manual check)

| Situation | Required behavior |
|---|---|
| MT5 terminal not connected | Account strip shows DISCONNECTED, values "—", execution disabled, retry with backoff, no exception |
| Engine heartbeat stale/offline/halted/paused | Badge with reason, execution disabled, close-position still allowed only if the engine supports it in that state |
| Market closed / no fresh tick | "MARKET CLOSED / STALE TICK" badge, Submit disabled |
| Margin used = 0 | Margin level "N/A" (never divide by zero) |
| Equity ≤ 0 or None | Risk % "N/A", Submit disabled |
| SL missing/0 | Blocked (`SL_REQUIRED`) |
| Lot below min / not on step | Auto-normalized with visible note; 0 after normalize blocks |
| `order_calc_margin` returns None | Hard block, message "cannot verify margin" |
| Symbol not in whitelist or missing from Market Watch | Not selectable / clear error |
| Category changed | `selected_asset` reset to valid member |
| Double click / Streamlit rerun mid-submit | Idempotency token + in-flight disable; at most one order |
| Position closed elsewhere between render and click | `close_position` returns `NOT_FOUND`, UI refreshes without error |
| Empty positions | Fixed-schema empty DataFrame + empty-state message |
| Journal/DB locked | Short retry, then degraded result, never crash |
| News feed down/malformed/HTML | Cached fallback + banner, parser never raises |
| Timezone/DST | All internal timestamps tz-aware UTC; convert only at display (IST) |
| Config changed since last session | State validated against whitelist at init (§4) |
| Narrow viewport | Columns stack; matrix scrolls horizontally |

---

## 11. TESTING AND VERIFICATION

- **Unit (pytest):** `risk_calc` (worked examples in §7, zero/None guards, rounding at step boundaries, JPY pair, non-forex points mode); `state.init_state` (defaults, validation against changed whitelist); `news.parse_calendar_xml` with fixtures: valid, truncated XML, empty root, missing fields, HTML body, entity-bomb payload, `All Day` and `Tentative` times; `data_access` degraded paths using the existing `tests/test_engine/fake_mt5.py`.
- **Streamlit AppTest** (`streamlit.testing.v1.AppTest`): app boots with a fake disconnected broker without exceptions; category change resets asset; Submit is disabled when engine is halted.
- **Manual checklist (screenshots attached to the PR):** (1) side-by-side with `echofi_reference.png`; (2) switch views and back — all choices persist; (3) 5 minutes running with a live demo account — no visible flicker, no layout jumps; (4) disconnect MT5 — graceful degraded UI; (5) block network — news falls back to cache; (6) place and close a **demo** order end-to-end; (7) verify rejected orders show the engine's reason.
- Run `scripts/secret_scan.py`; confirm no new file contains credentials.

---

## 12. DEFINITION OF DONE

- All phases passed their gates; `pytest` green; baseline tests unchanged except for intentional updates, each explained.
- No new positions store; no order path bypasses the engine/risk pipeline.
- `app.py` is a thin orchestrator; all new logic in `ui/`.
- Every number on screen is either live from the broker/engine or explicitly marked "—"/"N/A"/"STALE".
- `docs/ui.md` documents the theme tokens, state keys, data-access contract, and how to add a new component.
- Final report lists: files added/changed, deviations from this brief with reasons, known limitations, and anything you could not verify (e.g. unsupported order types).

## 13. DO NOT

- Do not hardcode `EUR/USD`, `100000` equity, `USD`, or any pip value.
- Do not use `.st-emotion-cache-*` selectors.
- Do not poll with `time.sleep`/`st.rerun` loops.
- Do not catch exceptions silently; log them with context, show a user-safe message.
- Do not add dependencies beyond `defusedxml` without asking; pin versions in `requirements.txt`.
- Do not commit `.env`, `.db` files, or screenshots that expose account numbers.