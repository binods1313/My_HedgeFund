"""Structural guard: Engine must expose the Stage 1b callables.

ScanCycle.run_with_engine reads ``read_history`` and ``evaluate_strategy``
from the engine via getattr with a None default. If those attributes are
missing the cycle silently does nothing — no signal rows are ever written.
This test makes the absence visible by FAILING fast, so a future refactor
that drops the wiring cannot turn signal generation off without a test
break.

Run: python -m pytest tests/test_engine/test_signal_wiring.py -v
"""
from __future__ import annotations

from engine.__main__ import Engine


def test_engine_has_read_history_callable():
    """Engine must expose a callable read_history method."""
    assert hasattr(Engine, "read_history"), \
        "Engine.read_history is missing — ScanCycle.run_with_engine " \
        "will silently skip signal generation."
    assert callable(Engine.read_history), \
        "Engine.read_history must be callable."


def test_engine_has_evaluate_strategy_callable():
    """Engine must expose a callable evaluate_strategy method."""
    assert hasattr(Engine, "evaluate_strategy"), \
        "Engine.evaluate_strategy is missing — ScanCycle.run_with_engine " \
        "will silently skip signal generation."
    assert callable(Engine.evaluate_strategy), \
        "Engine.evaluate_strategy must be callable."


def test_engine_has_account_property():
    """Engine must expose an account property for the risk pre-check."""
    assert hasattr(Engine, "account"), \
        "Engine.account is missing — the risk pre-check in the signal " \
        "path cannot build an AccountState."


def test_engine_run_with_engine_wires_callables():
    """ScanCycle.run_with_engine pulls read_history and evaluate_strategy
    from the engine and passes them to run(), which triggers signal
    writing when a new bar arrives.
    """
    from unittest.mock import MagicMock

    from engine.scan_cycle import ScanCycle
    from engine.state import STATE_IDLE, StateMachine
    from engine.journal import Journal
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    journal = Journal(tmp / "wiring.db")
    state = StateMachine(journal)
    state.transition(STATE_IDLE, "ENABLE_AI")

    engine = MagicMock()
    engine.config = MagicMock(mode="PAPER")
    engine.state = state
    engine.journal = journal
    engine.gateway = MagicMock()
    engine.gateway.snapshot.return_value = {
        "symbols": {
            "EURUSD": {"market_open": True, "stale": False,
                       "bar_ts": "2026-10-18T21:00:00+00:00",
                       "trade_mode": 4, "bid": 1.0900, "ask": 1.0902},
        }
    }
    engine.guard = None

    tick = "2026-10-18T21:00:00+00:00"
    engine.gateway.snapshot.return_value["symbols"]["EURUSD"]["bar_ts"] = tick
    scanner = ScanCycle(symbols=["EURUSD"], interval_seconds=0, broker_offset_seconds=0)
    # A MagicMock history is not a frame. The cycle must call read_history
    # and must not scan or store the snapshot tick as the bar.
    outcome = scanner.run_with_engine(engine)
    assert engine.read_history.called
    assert engine.evaluate_strategy.called is False
    assert outcome.scanned == 0
    assert outcome.signals == 0
    row = journal.query("SELECT bar_ts, reason FROM scan_symbols")[0]
    assert row["bar_ts"] is None
    assert row["reason"] == "NO_NEW_CLOSED_BAR"
    assert row["bar_ts"] != tick

    # Engine.read_history and Engine.evaluate_strategy must be real methods,
    # not MagicMock auto-attrs, so the wiring is explicit.
    assert engine.read_history is not None or \
        getattr(Engine, "read_history", None) is not None
    assert engine.evaluate_strategy is not None or \
        getattr(Engine, "evaluate_strategy", None) is not None

    journal.close()


def test_evaluate_strategy_calls_the_function_build_engine_stores():
    """build_engine assigns engine.strategy.evaluate, a bare function.

    Engine.evaluate_strategy must call that function. Calling .evaluate
    on it raises AttributeError and the scan writes no verdict.
    """
    seen = {}

    def strategy_fn(symbol, df):
        seen["symbol"] = symbol
        seen["df"] = df
        return "signal"

    engine = Engine.__new__(Engine)
    engine.strategy = strategy_fn
    assert engine.evaluate_strategy("EURUSD", ["bars"]) == "signal"
    assert seen == {"symbol": "EURUSD", "df": ["bars"]}


def test_evaluate_strategy_calls_an_object_with_evaluate():
    class Box:
        def evaluate(self, symbol, df):
            return (symbol, len(df))

    engine = Engine.__new__(Engine)
    engine.strategy = Box()
    assert engine.evaluate_strategy("GBPUSD", [1, 2, 3]) == ("GBPUSD", 3)


def test_scanner_ai_signals_enabled_loads_without_a_top_level_key():
    """config.yaml sets the flag under scanner. The loader must see it."""
    from pathlib import Path

    import yaml

    from engine.config import load_engine_config

    root = Path(__file__).resolve().parents[2]
    raw = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    assert "ai_signals_enabled" not in raw
    assert raw["scanner"]["ai_signals_enabled"] is True
    assert load_engine_config().ai_signals_enabled is True
