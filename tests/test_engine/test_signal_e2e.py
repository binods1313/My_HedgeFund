"""Stage 1b signal path E2E with mocked gateway and strategy.

Exercises the full chain without a live terminal:
  Engine.read_history  → ScanCycle._try_signal  → evaluate_strategy  →
  risk pre-check  → _write_signal_row  → signals table + lifecycle events

Three scenarios are covered:
  - A no-signal bar (strategy returns None): no row written, 0 signals.
  - An actionable signal that passes risk: PENDING row written, 1 signal.
  - An actionable signal that fails risk: REJECTED row written, 1 signal.

Run: python -m pytest tests/test_engine/test_signal_e2e.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from engine.journal import Journal
from engine.risk import RiskEngine, AccountState
from engine.scan_cycle import ScanCycle, _MIN_BARS_FOR_SIGNAL
from engine.state import STATE_IDLE, StateMachine
from engine.strategy import Signal, Block


def _make_history() -> pd.DataFrame:
    """206 closed M15 bars of flat price with a recent low sweep.

    Prices are forex-scale (around 1.0900) so signal.price and the
    derived stop loss are on the same numeric scale as the LotSizer
    tick spec.
    """
    n = _MIN_BARS_FOR_SIGNAL
    base = [1.0900 + i * 0.0001 for i in range(n)]
    low = [b - 0.0010 for b in base]
    high = [b + 0.0010 for b in base]
    return pd.DataFrame({
        "time": range(n),
        "open": base,
        "high": high,
        "low": low,
        "close": base,
        "tick_volume": [100] * n,
    })


def _make_df_with_sweep() -> pd.DataFrame:
    """Bars where the last close is below the prior low — a liquidity sweep."""
    n = _MIN_BARS_FOR_SIGNAL
    base = [1.0900 + i * 0.0001 for i in range(n)]
    low = [b - 0.0010 for b in base]
    # Last bar sweeps the prior low and closes back above it
    low[-1] = base[-2] - 0.0015
    high = [b + 0.0010 for b in base]
    close = list(base)
    close[-1] = low[-1] + 0.0005  # closes above the swept low
    return pd.DataFrame({
        "time": range(n),
        "open": base,
        "high": high,
        "low": low,
        "close": close,
        "tick_volume": [100] * n,
    })


class _MockGateway:
    """Minimal gateway stand-in: one snapshot, one history read."""

    connected = True

    def __init__(self, bar_ts: str, bid: float, ask: float, df: pd.DataFrame):
        self._bar_ts = bar_ts
        self._bid = bid
        self._ask = ask
        self._df = df
        self.read_history_calls: list[str] = []

    def snapshot(self, symbols: list[str]) -> dict:
        return {"symbols": {
            s: {"market_open": True, "stale": False,
                "bar_ts": self._bar_ts, "trade_mode": 4,
                "bid": self._bid, "ask": self._ask}
            for s in symbols
        }}

    def read_history(self, symbol: str) -> pd.DataFrame | None:
        self.read_history_calls.append(symbol)
        return self._df

    def symbol_info(self, symbol: str):
        info = type("Info", (), {})()
        info.digits = 5
        info.point = 1e-5
        info.volume_min = 0.01
        info.volume_max = 100.0
        info.volume_step = 0.01
        info.trade_tick_value = 1.0
        info.trade_tick_value_loss = 1.0
        info.trade_tick_size = 0.00001
        info.trade_contract_size = 100000.0
        return info


class _MockRiskEngine:
    """Risk engine stand-in that can be toggled approved/rejected."""

    def __init__(self, approved: bool = True):
        self._approved = approved

    def build_order_intent(self, **kwargs):
        intent = type("Intent", (), {})()
        intent.tick_size = 0.00001
        intent.risk_amount = 100.0
        intent.has_stop = True
        return intent

    def evaluate(self, intent, account) -> object:
        decision = type("Decision", (), {})()
        decision.approved = self._approved
        decision.reasons = [] if self._approved else ["test_reject"]
        return decision


class _MockConfig:
    def __init__(self, caps: dict | None = None, mult: int = 10):
        self.mode = "PAPER"
        self.volume_caps = caps or {}
        self.volume_cap_default_multiplier = mult


class _MockEquityTracker:
    def current(self) -> object | None:
        baselines = type("B", (), {})()
        baselines.start_of_day_equity = 100000.0
        baselines.peak_equity = 100000.0
        return baselines


class _MockEngine:
    """Hand-built engine exposing the callables ScanCycle.run_with_engine
    reads via getattr."""

    def __init__(self, gateway, strategy_eval, risk, journal, config,
                 equity_tracker, account_verified=True, account_balance=100000.0):
        self.gateway = gateway
        self.strategy = strategy_eval
        self.risk = risk
        self.journal = journal
        self.state = StateMachine(journal)
        self.state.transition(STATE_IDLE, "ENABLE_AI")
        self.config = config
        self.equity_tracker = equity_tracker
        self._account_verified = account_verified
        self.guard = None

    def read_history(self, symbol: str):
        return self.gateway.read_history(symbol)

    def evaluate_strategy(self, symbol: str, df):
        if self.strategy is None:
            return None
        return self.strategy(symbol, df)

    @property
    def account(self):
        if not self._account_verified:
            return None
        state = AccountState(
            equity=100000.0,
            start_of_day_equity=100000.0,
            peak_equity=100000.0,
            open_trade_count=0,
            total_open_risk=0.0,
        )
        return state

    @property
    def account_balance(self):
        return 100000.0


def _new_engine(tmp_path, *, approved=True, strategy_fn=None,
                volume_caps=None, df=None, bar_ts="2026-10-18T21:00:00+00:00"):
    journal = Journal(tmp_path / "signal_e2e.db")
    risk = _MockRiskEngine(approved=approved)
    gateway = _MockGateway(
        bar_ts=bar_ts, bid=1.0900, ask=1.0902,
        df=df if df is not None else _make_history(),
    )
    config = _MockConfig(caps=volume_caps or {})
    equity_tracker = _MockEquityTracker()

    # Strategy evaluator: wraps the test's strategy_fn, or a no-signal default.
    def evaluate_strategy(symbol, df):
        if strategy_fn is not None:
            return strategy_fn(symbol, df)
        return None

    engine = _MockEngine(
        gateway=gateway,
        strategy_eval=evaluate_strategy,
        risk=risk,
        journal=journal,
        config=config,
        equity_tracker=equity_tracker,
    )
    return journal, engine


def test_e2e_no_signal_produces_no_row(tmp_path):
    """Strategy returns None → no signal rows, 0 signals."""
    journal, engine = _new_engine(tmp_path, strategy_fn=lambda s, df: None)
    scanner = ScanCycle(symbols=["EURUSD"], interval_seconds=0)

    outcome = scanner.run_with_engine(engine)

    assert outcome.scanned == 1
    assert outcome.signals == 0
    rows = journal.query("SELECT status FROM signals ORDER BY id")
    assert len(rows) == 0


def test_e2e_actionable_signal_passes_risk(tmp_path):
    """Strategy returns actionable BUY, risk approves → PENDING row written."""
    def strategy_fn(symbol, df):
        return Signal(
            symbol=symbol, direction="BUY",
            price=1.0900,
            blocks=[Block("test", True, "ok")],
            confidence=0.9,
        )

    journal, engine = _new_engine(
        tmp_path, approved=True, strategy_fn=strategy_fn,
        df=_make_df_with_sweep(),
    )
    scanner = ScanCycle(symbols=["EURUSD"], interval_seconds=0)

    outcome = scanner.run_with_engine(engine)

    assert outcome.scanned == 1
    assert outcome.signals == 1

    rows = journal.query("SELECT symbol, direction, status, decision_id FROM signals ORDER BY id")
    assert len(rows) == 1
    assert rows[0]["status"] == "PENDING"
    assert rows[0]["direction"] == "BUY"
    assert rows[0]["symbol"] == "EURUSD"

    # Lifecycle event must be recorded
    events = journal.query(
        "SELECT category FROM events WHERE category = 'signal_generated' ORDER BY id")
    assert len(events) == 1

    journal.close()


def test_e2e_actionable_signal_fails_risk(tmp_path):
    """Strategy returns actionable SELL, risk rejects → REJECTED row written."""
    def strategy_fn(symbol, df):
        return Signal(
            symbol=symbol, direction="SELL",
            price=1.0900,
            blocks=[Block("test", True, "ok")],
            confidence=0.8,
        )

    journal, engine = _new_engine(
        tmp_path, approved=False, strategy_fn=strategy_fn,
        df=_make_df_with_sweep(),
    )
    scanner = ScanCycle(symbols=["EURUSD"], interval_seconds=0)

    outcome = scanner.run_with_engine(engine)

    assert outcome.scanned == 1
    assert outcome.signals == 1

    rows = journal.query("SELECT status, reject_reason FROM signals ORDER BY id")
    assert len(rows) == 1
    assert rows[0]["status"] == "REJECTED"
    assert rows[0]["reject_reason"] is not None

    events = journal.query(
        "SELECT category FROM events WHERE category = 'signal_rejected' ORDER BY id")
    assert len(events) == 1

    journal.close()


def test_e2e_insufficient_history(tmp_path):
    """read_history returns fewer than _MIN_BARS_FOR_SIGNAL → no signal row."""
    def strategy_fn(symbol, df):
        return Signal(
            symbol=symbol, direction="BUY",
            price=1.0900,
            blocks=[Block("test", True, "ok")],
            confidence=0.9,
        )

    short_df = _make_history().iloc[-10:]  # only 10 bars
    journal, engine = _new_engine(
        tmp_path, approved=True, strategy_fn=strategy_fn,
        df=short_df,
    )
    scanner = ScanCycle(symbols=["EURUSD"], interval_seconds=0)

    outcome = scanner.run_with_engine(engine)

    assert outcome.scanned == 1
    assert outcome.signals == 0
    rows = journal.query("SELECT * FROM signals ORDER BY id")
    assert len(rows) == 0

    journal.close()


def test_e2e_deterministic_decision_id(tmp_path):
    """Same symbol + bar_ts → same decision_id (no duplicate PENDING)."""
    def strategy_fn(symbol, df):
        return Signal(
            symbol=symbol, direction="BUY",
            price=1.0900,
            blocks=[Block("test", True, "ok")],
            confidence=0.9,
        )

    journal, engine = _new_engine(
        tmp_path, approved=True, strategy_fn=strategy_fn,
        df=_make_df_with_sweep(),
        bar_ts="2026-10-18T21:00:00+00:00",
    )
    scanner = ScanCycle(symbols=["EURUSD"], interval_seconds=0)

    # First cycle: new bar → signal written
    out1 = scanner.run_with_engine(engine)
    assert out1.signals == 1

    rows1 = journal.query("SELECT decision_id, status FROM signals ORDER BY id")
    assert len(rows1) == 1
    first_decision_id = rows1[0]["decision_id"]

    # Second cycle: same bar_ts → bar unchanged → skipped, no new signal
    out2 = scanner.run_with_engine(engine)
    assert out2.skipped == 1
    assert out2.signals == 0

    # Should still be exactly one signal row
    rows2 = journal.query("SELECT decision_id FROM signals ORDER BY id")
    assert len(rows2) == 1
    assert rows2[0]["decision_id"] == first_decision_id

    journal.close()


def test_e2e_history_read_once_per_new_bar(tmp_path):
    """read_history called exactly once per new bar, not per cycle."""
    def strategy_fn(symbol, df):
        return None  # no signal, but history is still read

    journal, engine = _new_engine(
        tmp_path, approved=True, strategy_fn=strategy_fn,
    )

    bar_ts_1 = "2026-10-18T21:00:00+00:00"
    bar_ts_2 = "2026-10-18T21:15:00+00:00"

    scanner = ScanCycle(symbols=["EURUSD"], interval_seconds=0)

    # Cycle 1: new bar
    engine.gateway._bar_ts = bar_ts_1
    scanner.run_with_engine(engine)
    assert len(engine.gateway.read_history_calls) == 1

    # Cycle 2: same bar → no read
    scanner.run_with_engine(engine)
    assert len(engine.gateway.read_history_calls) == 1

    # Cycle 3: new bar → one more read
    engine.gateway._bar_ts = bar_ts_2
    scanner.run_with_engine(engine)
    assert len(engine.gateway.read_history_calls) == 2

    journal.close()
