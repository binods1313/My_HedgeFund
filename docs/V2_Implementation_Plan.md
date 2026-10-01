# VERSION 2.0 — Implementation Plan
## Institutional AI Trading Terminal

> **Based on:** `Instructions_1.md` developer brief + codebase analysis (`app.py`, `data_engine.py`, `execution.py`, `ai_brain.py`, `config.py`)

---

## EXECUTIVE SUMMARY

The current system is a working **Streamlit + MT5 + LLM prototype** with one critical architectural flaw: **all trading logic runs inside Streamlit reruns**. The engine dies when the browser tab closes, cannot run autonomously, and cannot scan multiple symbols concurrently. V2.0 separates the UI from the engine entirely and adds a proper risk-gated, quantitative pipeline where the LLM is a validation layer — not the strategy.

---

## TARGET DIRECTORY STRUCTURE

```
HedgeFund_Binod/
├── engine/
│   ├── __main__.py                 # Entry: python -m engine
│   ├── state.py                    # State machine + heartbeat writer
│   ├── gateway/
│   │   └── mt5_gateway.py          # Single-thread MT5 owner; all others use queue
│   ├── scanner/
│   │   └── market_scanner.py       # Async multi-symbol scanner
│   ├── strategies/
│   │   ├── base.py                 # Strategy interface (Signal dataclass)
│   │   ├── trend.py                # EMA/ADX alignment strategy
│   │   ├── momentum.py             # RSI/MACD momentum strategy
│   │   ├── breakout.py             # Structure breakout strategy
│   │   └── smart_money.py          # FVG/Order Block/MSS strategy
│   ├── confidence/
│   │   └── confidence_engine.py    # Computed composite 0-100 score
│   ├── risk/
│   │   ├── risk_engine.py          # Gatekeeper: APPROVED / REJECTED
│   │   └── position_sizer.py       # Dynamic lot calculation
│   ├── llm/
│   │   └── llm_validator.py        # Refactored ai_brain.py — validation ONLY
│   ├── executor/
│   │   └── order_executor.py       # Refactored execution.py
│   ├── manager/
│   │   └── trade_manager.py        # Breakeven, trailing, partial close
│   └── journal/
│       └── journal_db.py           # SQLite WAL mode
├── ui/
│   └── app.py                      # Streamlit UI — reads DB only, no trading logic
├── analytics/
│   └── analytics.py
├── backtesting/
│   └── backtest_engine.py
├── config.yaml                     # All runtime config (replaces config.py)
├── .env                            # Secrets — gitignored
├── requirements.txt
└── tests/
    ├── test_risk_engine.py
    ├── test_position_sizing.py
    ├── test_strategies.py
    └── test_llm_validator.py
```

---

## PHASE 0 — Critical Bug Fixes (Start Here)

These bugs exist in the current codebase. Fix before any V2 work.

### Bug 1: Invalid Volume (Code 10014)
**File:** `execution.py` line 152

```python
# CURRENT — passes raw user float, not normalized
"volume": float(lot_size),

# FIX — normalize to broker rules
import math
info = mt5.symbol_info(symbol)
lot = math.floor(lot_size / info.volume_step) * info.volume_step
lot = max(info.volume_min, min(info.volume_max, lot))
if lot < info.volume_min:
    return {"success": False, "message": f"Lot below broker minimum {info.volume_min}"}
"volume": lot,
```

### Bug 2: Wrong Symbol Classification
**File:** `data_engine.py` line 65 — substring "ETH" matches "AETH" (a stock ETF)

```python
# CURRENT — name substring matching (wrong)
crypto = [s for s in symbol_names if any(x in s for x in ["BTC", "ETH", ...])]

# FIX — use MT5 symbol path/sector
def classify_symbol(sym_info) -> str:
    path = (sym_info.path or "").upper()
    if "FOREX" in path or "FX" in path:       return "Forex Currencies"
    if "CRYPTO" in path:                        return "Crypto Assets"
    if "METAL" in path or "COMMODITY" in path: return "Metals & Commodities"
    if "INDEX" in path or "INDICES" in path:   return "Indices & Equities"
    if "STOCK" in path or "EQUITY" in path:    return "Indices & Equities"
    return "Other"
```

### Bug 3: Corrupt Spread Calculation (4543.0 pips)
**File:** `data_engine.py` line 174

```python
# CURRENT — applies * 10 heuristic (wrong for gold, indices, crypto)
spread = (tick.ask - tick.bid) / (info.point * 10 if info.digits in (3,5) else info.point)

# FIX — use point directly; validate tick sanity first
if tick.ask < tick.bid or (tick.ask - tick.bid) > tick.ask * 0.05:
    return {"bid": 0.0, "ask": 0.0, "spread": 0.0, "valid": False, "reason": "Corrupt quote"}
spread_points = round((tick.ask - tick.bid) / info.point, 1)
```

### Bug 4: Currency Label
**File:** `app.py` HUD bar — shows `$100,000.00 EUR` ($ hardcoded)

```python
# FIX
sym = {"USD":"$","EUR":"€","GBP":"£","JPY":"¥"}.get(telemetry["currency"], "")
f"{sym}{bal:,.2f} {telemetry['currency']}"
```

### Bug 5: Meaningless Margin Level (487,572.9%)
**File:** `app.py` HUD bar — margin level is infinite when margin used ≈ $0.51

```python
# FIX
margin_level_str = f"{ml:,.1f}%" if ml > 0 and telemetry["margin"] > 1.0 else "N/A"
```

### Bug 6: LLM Hallucination — Unstructured Text Input
**File:** `ai_brain.py` line 41, `data_engine.py` line 132

```python
# CURRENT — free-form text string; LLM invents numbers
{"role": "user", "content": market_data_string}

# FIX — structured JSON snapshot; validate LLM output
indicator_snapshot = {
    "symbol": symbol, "close": float(last["close"]),
    "ema20": float(last["ema20"]), "ema50": float(last["ema50"]),
    "rsi": float(last["rsi"]), "macd_hist": float(last["macd_hist"]),
    "atr": float(last["atr"]),
}
```

### Bug 7: Credentials in Source Code
**File:** `config.py` lines 2-9 — MT5 password and API key in plaintext

```python
# FIX — .env file (gitignored) + python-dotenv
# .env
MT5_LOGIN=113363533
MT5_PASSWORD=***REMOVED-MT5_PASSWORD***
OPENROUTER_API_KEY=sk-or-v1-...

# config.py
from dotenv import load_dotenv; import os
load_dotenv()
MT5_LOGIN = int(os.environ["MT5_LOGIN"])
MT5_PASSWORD = os.environ["MT5_PASSWORD"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
```

---

## PHASE 1 — Separate Engine Process + State Machine

### Key Rule
**Streamlit only reads the SQLite DB.** No MT5 calls, no strategy logic, no LLM calls inside Streamlit reruns.

### `engine/state.py`
```python
from enum import Enum
import time, sqlite3

class EngineState(Enum):
    OFF = "OFF"; IDLE = "IDLE"; SCANNING = "SCANNING"
    ANALYZING = "ANALYZING"; VALIDATING = "VALIDATING"
    EXECUTING = "EXECUTING"; MONITORING = "MONITORING"
    PAUSED = "PAUSED"; EMERGENCY_STOP = "EMERGENCY_STOP"

class StateManager:
    def set_state(self, state: EngineState, reason="", symbol=""):
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO engine_state (id,state,reason,symbol,heartbeat)
                VALUES (1,?,?,?,?)
            """, (state.value, reason, symbol, time.time()))
```

### `engine/gateway/mt5_gateway.py`
```python
import queue, threading, MetaTrader5 as mt5

class MT5Gateway:
    """Single thread owns the MT5 API. All modules submit via queue."""
    def __init__(self):
        self._q = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, fn, *args, **kwargs):
        ev = threading.Event(); box = [None, None]
        self._q.put((fn, args, kwargs, box, ev))
        ev.wait(timeout=10)
        if box[1]: raise box[1]
        return box[0]

    def _run(self):
        while True:
            fn, a, kw, box, ev = self._q.get()
            try: box[0] = fn(*a, **kw)
            except Exception as e: box[1] = e
            finally: ev.set()
```

---

## PHASE 2 — Autonomous Market Scanner

```python
class MarketScanner:
    WATCHLIST = ["EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD",
                 "XAUUSD","XAGUSD","BTCUSD","ETHUSD","NAS100","US30","GER40"]

    def scan_all(self, gateway) -> list:
        results = []
        for sym in self.WATCHLIST:
            quote = gateway.submit(get_symbol_live_quote, sym)
            if not quote.get("valid", True) or quote["bid"] == 0:
                results.append({"symbol": sym, "valid": False, "reason": quote.get("reason","")})
                continue
            df = gateway.submit(get_technical_dataframe, sym, TIMEFRAME_M15, 100)
            if df.empty: continue
            scores = StrategyEngine.score_all(df)
            conf = ConfidenceEngine.compute(scores)
            results.append({"symbol":sym,"valid":True,"direction":scores["direction"],
                            "ai_confidence":conf,"final_rank":conf,"key_reason":scores["top_reason"]})
        # Top 10 ranked, excludes invalid
        valid = sorted([r for r in results if r["valid"]], key=lambda x: x["final_rank"], reverse=True)
        return valid[:10]
```

**Acceptance:** Full watchlist scan completes in < 10 seconds per cycle. Never blocks the UI.

---

## PHASE 3 — Risk Engine V2

```python
class RiskEngine:
    """No order bypasses this. Every decision is logged."""

    def approve(self, signal, account, journal) -> tuple[bool, str]:
        for check, args in [
            (self._daily_loss, [account]),
            (self._max_drawdown, [account]),
            (self._max_open_trades, [journal]),
            (self._spread_filter, [signal]),
            (self._volatility_filter, [signal]),
            (self._margin_check, [signal, account]),
        ]:
            reason = check(*args)
            if reason:
                journal.log_risk_decision("REJECTED", signal.symbol, reason)
                return False, reason

        journal.log_risk_decision("APPROVED", signal.symbol, "All checks passed")
        return True, "APPROVED"

    def _daily_loss(self, account) -> str | None:
        loss_pct = (account.start_equity - account.equity) / account.start_equity
        if loss_pct >= self.cfg["daily_loss_limit"]:  # default 0.03
            return f"Daily loss limit {loss_pct:.1%} >= {self.cfg['daily_loss_limit']:.1%}"

    def _max_open_trades(self, journal) -> str | None:
        if len(journal.get_open_trades()) >= self.cfg["max_open_trades"]:  # default 5
            return f"Max open trades ({self.cfg['max_open_trades']}) reached"

    def _spread_filter(self, signal) -> str | None:
        max_pts = self.cfg["symbols"].get(signal.symbol, {}).get("max_spread_points", 50)
        if signal.spread_points > max_pts:
            return f"Spread {signal.spread_points} > max {max_pts} points"
```

**Limits (configurable in config.yaml):**

| Limit | Default |
|---|---|
| Risk per trade | 1% of equity |
| Daily loss limit | 3% of start-of-day equity |
| Max drawdown | 10% from peak |
| Max open trades | 5 |
| Max total exposure | 10% of equity |

---

## PHASE 4 — Dynamic Position Sizing

```python
import math, MetaTrader5 as mt5

def calculate_lot(symbol: str, sl_distance: float, equity: float,
                  risk_pct: float = 0.01) -> tuple[float, str]:
    """
    Risk Capital = Equity x Risk%
    Lot = Risk Capital / (SL distance in price x tick_value / tick_size)

    Works for: Forex, JPY pairs, Gold, Indices, Crypto, cross-currency accounts.
    Never uses a hardcoded pip value.
    """
    info = mt5.symbol_info(symbol)
    risk_capital = equity * risk_pct
    ticks_in_sl = sl_distance / info.tick_size
    value_per_lot = ticks_in_sl * info.tick_value

    raw_lot = risk_capital / value_per_lot
    lot = math.floor(raw_lot / info.volume_step) * info.volume_step
    lot = max(info.volume_min, min(info.volume_max, lot))

    if lot < info.volume_min:
        return 0.0, f"Risk budget too small (min lot {info.volume_min} exceeds budget)"

    log = (f"Equity {equity:,.0f} → Risk {risk_pct:.1%} = {risk_capital:,.0f} | "
           f"SL dist {sl_distance:.5f} → {ticks_in_sl:.0f} ticks | "
           f"Value/lot {value_per_lot:.2f} | Raw {raw_lot:.4f} → Lot {lot:.2f}")
    return lot, log
```

**Unit tests required:**

| Scenario | Expected |
|---|---|
| EURUSD, $10k account, 50 pip SL, 1% risk | ~0.20 lots |
| USDJPY, $10k account (JPY quote currency) | Adjusted for JPY tick_value |
| XAUUSD, $10k account | Gold contract size |
| NAS100 index | Index point value |
| BTCUSD, $100 account | Returns 0.0 (below minimum) |

---

## PHASE 5 — Smart Trade Manager

Runs every second on all positions tagged `magic = 100100`.

```python
class TradeManager:
    def tick(self, gateway, journal):
        for pos in gateway.submit(mt5.positions_get) or []:
            if pos.magic != 100100: continue
            trade = journal.get_trade_by_ticket(pos.ticket)
            if not trade: continue
            sl_dist = abs(trade["entry"] - trade["stop_loss"])
            profit_r = abs(pos.price_current - trade["entry"]) / sl_dist if sl_dist else 0

            if not trade["be_moved"] and profit_r >= 1.0:
                self._move_to_breakeven(pos, trade, gateway, journal)
            if not trade["partial_closed"] and profit_r >= 2.0:
                self._partial_close_and_trail(pos, trade, gateway, journal)

    def _move_to_breakeven(self, pos, trade, gateway, journal):
        info = gateway.submit(mt5.symbol_info, pos.symbol)
        buffer = max(info.stops_level * info.point, info.point * 5)
        new_sl = (trade["entry"] + buffer if pos.type == 0
                  else trade["entry"] - buffer)
        request = {"action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket,
                   "sl": round(new_sl, info.digits), "tp": pos.tp}
        result = gateway.submit(mt5.order_send, request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            journal.mark_breakeven(pos.ticket)
```

---

## PHASE 6 — Trade Journal Database

### SQLite Schema (WAL mode)

```sql
CREATE TABLE trades (
    trade_id           TEXT PRIMARY KEY,
    mt5_ticket         INTEGER,
    magic              INTEGER,
    timestamp_open     REAL,
    timestamp_close    REAL,
    symbol             TEXT,
    asset_class        TEXT,
    strategy           TEXT,
    direction          TEXT,
    lot                REAL,
    entry              REAL,
    stop_loss          REAL,
    take_profit        REAL,
    exit_price         REAL,
    exit_reason        TEXT,
    pnl                REAL,
    commission         REAL,
    swap               REAL,
    r_multiple         REAL,
    confidence         REAL,
    strategy_scores    TEXT,       -- JSON
    indicator_snapshot TEXT,       -- JSON: exact values sent to LLM
    llm_verdict        TEXT,
    llm_reason         TEXT,
    slippage           REAL,
    spread_at_entry    REAL,
    be_moved           INTEGER DEFAULT 0,
    partial_closed     INTEGER DEFAULT 0
);

CREATE TABLE risk_decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL, symbol TEXT, verdict TEXT, reason TEXT, signal_json TEXT
);

CREATE TABLE llm_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL, symbol TEXT, prompt_hash TEXT,
    verdict         TEXT, confidence_adj REAL, latency_ms INTEGER, raw_response TEXT
);

CREATE TABLE equity_snapshots (ts REAL PRIMARY KEY, balance REAL, equity REAL, profit REAL);

CREATE TABLE engine_state (
    id INTEGER PRIMARY KEY DEFAULT 1,
    state TEXT, reason TEXT, symbol TEXT, heartbeat REAL
);
```

> **Critical:** Log rejected signals with full reason. This is required for calibrating thresholds from real data.

---

## PHASE 7 — Strategy Engine

### Base Interface

```python
from dataclasses import dataclass, field
from abc import ABC, abstractmethod
import pandas as pd

@dataclass
class Signal:
    direction: str          # BUY | SELL | NONE
    entry: float
    stop_loss: float        # Always set from ATR/structure, never user-typed
    take_profit: float
    score: float            # 0-100
    reasons: list[str] = field(default_factory=list)
    indicator_values: dict = field(default_factory=dict)  # sent verbatim to LLM

class Strategy(ABC):
    name: str

    @abstractmethod
    def evaluate(self, df_entry: pd.DataFrame, df_htf: pd.DataFrame) -> Signal:
        """
        df_entry = 15M (entry TF), df_htf = 4H (bias TF).
        ALWAYS use df.iloc[-2] (last CLOSED bar), never df.iloc[-1].
        No look-ahead. No repainting.
        """
        ...
```

### Trend Strategy

```python
class TrendStrategy(Strategy):
    name = "Trend"

    def evaluate(self, df_entry, df_htf) -> Signal:
        bar = df_entry.iloc[-2]   # last CLOSED bar
        htf = df_htf.iloc[-2]

        bull = bar["close"] > bar["ema20"] > bar["ema50"] and htf["close"] > htf["ema50"]
        bear = bar["close"] < bar["ema20"] < bar["ema50"] and htf["close"] < htf["ema50"]
        adx  = bar.get("adx", 0)

        direction = "BUY" if bull and adx > 25 else "SELL" if bear and adx > 25 else "NONE"
        score = min(100, 40 + adx) if direction != "NONE" else 0
        sl_dist = bar["atr"] * 1.5  # ATR-based, not user input
        entry = bar["close"]
        sl = entry - sl_dist if direction == "BUY" else entry + sl_dist
        tp = entry + sl_dist * 2 if direction == "BUY" else entry - sl_dist * 2

        return Signal(direction=direction, entry=entry, stop_loss=sl, take_profit=tp,
                      score=score,
                      reasons=[f"EMA aligned {'bullish' if direction=='BUY' else 'bearish'}",
                                f"ADX {adx:.1f}"],
                      indicator_values={"ema20": bar["ema20"], "ema50": bar["ema50"],
                                        "adx": adx, "atr": bar["atr"], "close": bar["close"]})
```

---

## PHASE 8 — AI Confidence Engine

```python
class ConfidenceEngine:
    """Replaces LLM-asserted confidence. All values computed from indicators."""

    WEIGHTS = {
        "trend": 0.25, "momentum": 0.25,
        "volatility": 0.15, "volume": 0.15, "structure": 0.20
    }

    @classmethod
    def compute(cls, scores: dict) -> float:
        return round(sum(scores.get(k, 0) * w for k, w in cls.WEIGHTS.items()), 1)

    @staticmethod
    def trend_score(df) -> float:
        bar = df.iloc[-2]
        c, e20, e50 = bar["close"], bar["ema20"], bar["ema50"]
        if c > e20 > e50: return min(100, 60 + abs(c - e50) / e50 * 1000)
        if c < e20 < e50: return min(100, 60 + abs(c - e50) / e50 * 1000)
        return 20.0

    @staticmethod
    def momentum_score(df) -> float:
        bar = df.iloc[-2]
        rsi_score = abs(bar["rsi"] - 50) * 2
        macd_boost = 1.2 if bar["macd_hist"] > 0 else 0.8
        return min(100, rsi_score * macd_boost)
```

**Execution threshold:** `confidence > 85` (configurable per asset class). Log the score distribution for the first 2 weeks. If < 5% of scans exceed 85, calibrate the threshold from data.

---

## PHASE 9 — LLM Validation Layer (Refactored `ai_brain.py`)

```python
class LLMValidator:
    """Validation only. Cannot create a trade. Fails safe on any error."""

    OUTPUT_SCHEMA = {
        "verdict": ["APPROVE", "REJECT", "REDUCE"],
        "confidence_adjust": (-20, 20),
        "reasons": list,
    }

    def validate(self, signal: Signal) -> LLMDecision:
        payload = {
            "signal": signal.direction,
            "score": signal.score,
            "indicators": signal.indicator_values,  # exact computed values
            "reasons": signal.reasons,
        }
        try:
            raw = self._call_api(json.dumps(payload), timeout=15)
            parsed = self._parse_validate(raw)
        except Exception as e:
            # FAIL SAFE — any error = no trade
            self._log_call(signal.symbol, error=str(e))
            return LLMDecision(verdict="REJECT", reason=f"LLM fail-safe: {e}")

        self._log_call(signal.symbol, parsed=parsed)
        return LLMDecision(
            verdict=parsed["verdict"],
            confidence_adjust=parsed["confidence_adjust"],
            reasons=parsed["reasons"],
        )

    def _parse_validate(self, raw: str) -> dict:
        cleaned = re.sub(r"```json|```|<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        parsed = json.loads(cleaned)
        assert parsed["verdict"] in self.OUTPUT_SCHEMA["verdict"]
        adj = parsed["confidence_adjust"]
        lo, hi = self.OUTPUT_SCHEMA["confidence_adjust"]
        assert lo <= adj <= hi, f"confidence_adjust {adj} out of bounds"
        return parsed
```

---

## PHASE 10 — UI Upgrades

### ENABLE AI Toggle (top bar)

```python
state_row = read_engine_state_from_db()
STATE_COLORS = {"OFF":"#475569","ON":"#34D399","PAUSED":"#F59E0B","EMERGENCY_STOP":"#F43F5E"}
mode = get_config_from_db("trading_mode")  # PAPER or LIVE
badge_color = "#F43F5E" if mode == "LIVE" else "#22D3EE"

st.markdown(f"""
<div style="display:flex;align-items:center;gap:12px;padding:0.5rem 1rem;">
  <div style="background:{STATE_COLORS.get(state_row['state'],'#475569')};
              border-radius:8px;padding:6px 16px;font-weight:700;font-size:13px;">
    AI ENGINE: {state_row['state']}
  </div>
  <span style="color:{badge_color};font-weight:700;font-size:12px;
               border:1px solid {badge_color};padding:3px 10px;border-radius:12px;">
    {mode}
  </span>
  <span style="font-size:11px;color:#64748B;">{state_row['symbol']} | {state_row['reason']}</span>
</div>
""", unsafe_allow_html=True)
```

### Lightning Border (GPU-safe CSS)

```css
@property --angle {
  syntax: '<angle>';
  initial-value: 0deg;
  inherits: false;
}
.lightning-card { position: relative; border-radius: 14px; isolation: isolate; }
.lightning-card::before {
    content: '';
    position: absolute;
    inset: -1px;
    border-radius: inherit;
    background: conic-gradient(
        from var(--angle), transparent 70%, #22D3EE 80%, transparent 90%
    );
    animation: rotate-border 3s linear infinite;
    z-index: -1;
}
@keyframes rotate-border { to { --angle: 360deg; } }
@media (prefers-reduced-motion: reduce) {
    .lightning-card::before { animation: none; border: 1px solid #22D3EE; }
}
```

> Implementation note: Inject CSS once globally, not per-widget. Streamlit reruns must not restart animations or cause flicker.

---

## DELIVERABLES BY PHASE

| Phase | Deliverable | Acceptance Criterion |
|---|---|---|
| **0** | Bug fixes in current codebase | No 10014 errors; correct currency/margin labels; no LLM hallucinations |
| **1** | Engine process + gateway + state machine | Engine survives browser close; heartbeat updates DB every 5s |
| **2** | Risk Engine V2 + Position Sizing | Every order has logged APPROVED/REJECTED; all 5 unit test cases pass |
| **3** | Trade Journal DB | All signals, decisions, trades logged; rejected signals stored with reason |
| **4** | Strategy Engine (2 strategies min) | Backtestable; uses only closed bars; deterministic output |
| **5** | Autonomous Scanner | Full watchlist < 10s per cycle; exclusion reasons shown in UI |
| **6** | LLM Validation Layer | Fails safe on timeout/schema error; every call logged with latency |
| **7** | ENABLE AI toggle + state machine | Toggle ON → scanning begins; no manual clicks needed |
| **8** | Trade Manager | Breakeven at 1R; trailing + partial close at 2R |
| **9** | Analytics UI | Equity curve, win rate, per-strategy stats visible in dashboard |
| **10** | Lightning border UI + AI CORE panel | 60 FPS on mid-range laptop; no flicker on Streamlit rerun |

---

## KEY PRINCIPLE

> The LLM is **not** the trading strategy.
>
> Quantitative strategies → Confidence Engine → Risk Engine (veto) → LLM validation (fail-safe) → Executor.
>
> If any step after Confidence Engine fails, the default is **no trade** — never the opposite.
