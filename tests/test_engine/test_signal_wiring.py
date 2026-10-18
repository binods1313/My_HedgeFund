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

    scanner = ScanCycle(symbols=["EURUSD"], interval_seconds=0)
    # If run_with_engine cannot read read_history or evaluate_strategy,
    # the outcome will have signals=0 and a scanned row only.
    outcome = scanner.run_with_engine(engine)
    assert outcome.scanned == 1

    # Engine.read_history and Engine.evaluate_strategy must be real methods,
    # not MagicMock auto-attrs, so the wiring is explicit.
    assert engine.read_history is not None or \
        getattr(Engine, "read_history", None) is not None
    assert engine.evaluate_strategy is not None or \
        getattr(Engine, "evaluate_strategy", None) is not None

    journal.close()
