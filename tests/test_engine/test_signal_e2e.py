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


# Friday 16 Oct 2026 20:45 UTC. Divisible by 900, so it is an M15 bar open.
_BAR_OPEN = datetime(2026, 10, 16, 20, 45, tzinfo=timezone.utc)
_BAR_OPEN_EPOCH = int(_BAR_OPEN.timestamp())


def _m15_times(n: int, last: int = _BAR_OPEN_EPOCH) -> list[int]:
    return [last - 900 * (n - 1 - i) for i in range(n)]


def _make_history() -> pd.DataFrame:
    """206 closed M15 bars of flat price.

    The ``time`` column is the bar open epoch. The last open is M15-aligned.
    Prices are forex-scale (around 1.0900) so signal.price and the
    derived stop loss are on the same numeric scale as the LotSizer
    tick spec.
    """
    n = _MIN_BARS_FOR_SIGNAL
    base = [1.0900 + i * 0.0001 for i in range(n)]
    low = [b - 0.0010 for b in base]
    high = [b + 0.0010 for b in base]
    return pd.DataFrame({
        "time": _m15_times(n),
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
        "time": _m15_times(n),
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
    scanner = ScanCycle(symbols=["EURUSD"], interval_seconds=0, broker_offset_seconds=0)

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
    scanner = ScanCycle(symbols=["EURUSD"], interval_seconds=0, broker_offset_seconds=0)

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
    scanner = ScanCycle(symbols=["EURUSD"], interval_seconds=0, broker_offset_seconds=0)

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
    scanner = ScanCycle(symbols=["EURUSD"], interval_seconds=0, broker_offset_seconds=0)

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
    scanner = ScanCycle(symbols=["EURUSD"], interval_seconds=0, broker_offset_seconds=0)

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


def test_e2e_history_frame_not_the_tick_decides_the_bar(tmp_path):
    """The closed-bar open in the frame is the identity. The tick is not.

    A repeat of the same frame reads history again (that is how the cycle
    knows the bar) and does not write a second signal. Advancing the
    frame's last open is what counts as the next bar. Moving only the
    snapshot tick does nothing.
    """
    def strategy_fn(symbol, df):
        return None

    journal, engine = _new_engine(
        tmp_path, approved=True, strategy_fn=strategy_fn,
    )
    scanner = ScanCycle(
        symbols=["EURUSD"], interval_seconds=0, broker_offset_seconds=0,
    )
    tick = str(int(datetime(2026, 10, 16, 5, 17, 42, tzinfo=timezone.utc).timestamp()))
    engine.gateway._bar_ts = tick

    scanner.run_with_engine(engine)
    assert engine.gateway.read_history_calls == ["EURUSD"]
    row = journal.query("SELECT bar_ts, reason FROM scan_symbols")[0]
    assert int(row["bar_ts"]) == _BAR_OPEN_EPOCH
    assert row["bar_ts"] != tick

    scanner.run_with_engine(engine)
    assert len(engine.gateway.read_history_calls) == 2
    assert journal.query("SELECT COUNT(*) AS n FROM signals")[0]["n"] == 0

    engine.gateway._bar_ts = str(int(tick) + 60)
    next_open = _BAR_OPEN_EPOCH + 900
    frame = engine.gateway._df.copy()
    frame.loc[frame.index[-1], "time"] = next_open
    engine.gateway._df = frame
    scanner.run_with_engine(engine)
    assert len(engine.gateway.read_history_calls) == 3
    latest = journal.query("SELECT bar_ts FROM scan_symbols ORDER BY id DESC")[0]
    assert int(latest["bar_ts"]) == next_open

    journal.close()


def test_actionable_frame_is_pending_idempotent_and_superseded(tmp_path):
    """Real ScanCycle: one PENDING row, then the next bar supersedes it.

    No order function is called. decision_id is the closed bar open.
    expires_at is min(bar_close_utc + 900s, created_utc + 1800s).
    """
    import uuid

    from engine.scan_cycle import _SIGNAL_NAMESPACE, _STRATEGY_LABEL

    orders: list = []

    def send_order(*args, **kwargs):
        orders.append((args, kwargs))

    def strategy_fn(symbol, df):
        return Signal(
            symbol=symbol, direction="BUY", price=1.0900,
            blocks=[Block("test", True, "ok")], confidence=0.9,
        )

    created = "2026-10-16T21:00:10+00:00"
    journal, engine = _new_engine(
        tmp_path, approved=True, strategy_fn=strategy_fn,
        df=_make_df_with_sweep(),
        bar_ts=str(int(datetime(2026, 10, 16, 5, 17, 42, tzinfo=timezone.utc).timestamp())),
    )
    engine.gateway.send_order = send_order
    engine.gateway.order_send = send_order
    scanner = ScanCycle(
        symbols=["EURUSD"], interval_seconds=0, broker_offset_seconds=0,
    )

    def run_once(cycle: ScanCycle):
        return cycle.run(
            state=engine.state, journal=journal, mode="PAPER",
            read_market=engine.gateway.snapshot,
            read_history=engine.gateway.read_history,
            evaluate_strategy=engine.evaluate_strategy,
            now_iso=created,
        )

    first = run_once(scanner)
    assert first.signals == 1
    expected_id = str(uuid.uuid5(
        _SIGNAL_NAMESPACE, f"EURUSD|{_STRATEGY_LABEL}|{_BAR_OPEN_EPOCH}",
    ))
    # close 21:00:00 + 900s = 21:15:00; created+1800 = 21:30:10
    expected_expiry = "2026-10-16T21:15:00+00:00"
    rows = journal.query(
        "SELECT status, decision_id, bar_ts, expires_at FROM signals ORDER BY id")
    assert len(rows) == 1
    assert rows[0]["status"] == "PENDING"
    assert int(rows[0]["bar_ts"]) == _BAR_OPEN_EPOCH
    assert rows[0]["decision_id"] == expected_id
    assert rows[0]["expires_at"] == expected_expiry

    second = run_once(scanner)
    assert second.signals == 0
    assert len(journal.query("SELECT id FROM signals")) == 1
    assert orders == []
    assert journal.query("SELECT COUNT(*) AS n FROM trades")[0]["n"] == 0

    # A new process (empty bar memory) still does not insert a duplicate.
    fresh = ScanCycle(symbols=["EURUSD"], interval_seconds=0, broker_offset_seconds=0)
    third = run_once(fresh)
    assert third.signals == 0
    assert len(journal.query("SELECT id FROM signals")) == 1

    next_open = _BAR_OPEN_EPOCH + 900
    frame = _make_df_with_sweep()
    frame.loc[frame.index[-1], "time"] = next_open
    engine.gateway._df = frame
    nxt = ScanCycle(symbols=["EURUSD"], interval_seconds=0, broker_offset_seconds=0)
    fourth = run_once(nxt)
    assert fourth.signals == 1
    again = journal.query(
        "SELECT status, decision_id, bar_ts FROM signals ORDER BY id")
    assert [row["status"] for row in again] == ["SUPERSEDED", "PENDING"]
    assert int(again[1]["bar_ts"]) == next_open
    assert again[1]["decision_id"] != expected_id
    assert orders == []
    journal.close()


def test_unaligned_timestamp_is_not_used(tmp_path):
    """05:17:42 is a tick, not an M15 open. It must not be stored or hashed."""
    tick_dt = datetime(2026, 10, 16, 5, 17, 42, tzinfo=timezone.utc)
    tick = int(tick_dt.timestamp())
    assert tick % 900 != 0

    frame = _make_history()
    frame.loc[frame.index[-1], "time"] = tick

    def strategy_fn(symbol, df):
        return Signal(
            symbol=symbol, direction="BUY", price=1.0900,
            blocks=[Block("test", True, "ok")], confidence=1.0,
        )

    journal, engine = _new_engine(
        tmp_path, strategy_fn=strategy_fn, df=frame, bar_ts=str(tick),
    )
    scanner = ScanCycle(
        symbols=["EURUSD"], interval_seconds=0, broker_offset_seconds=0,
    )
    outcome = scanner.run_with_engine(engine)
    assert outcome.signals == 0
    assert outcome.scanned == 0
    row = journal.query("SELECT bar_ts, reason FROM scan_symbols")[0]
    assert row["bar_ts"] is None
    assert row["reason"] == "BAR_TIME_NOT_ALIGNED"
    assert journal.query("SELECT COUNT(*) AS n FROM signals")[0]["n"] == 0
    journal.close()
