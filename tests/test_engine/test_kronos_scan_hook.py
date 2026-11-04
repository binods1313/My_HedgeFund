"""Scan-cycle integration: Kronos hook is observational and fail-open.

Proves: a PENDING signal is written exactly the same whether the Kronos hook
is absent, raises, or produces an error — the hook can never alter or block
signal generation.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

from engine.journal import Journal
from engine.scan_cycle import ScanCycle
from engine.state import STATE_IDLE, StateMachine

STAMP = "2026-11-04T10:00:00+00:00"
BAR_TS = 1_750_000_000


def make_state(journal) -> StateMachine:
    state = StateMachine(journal)
    state.transition(STATE_IDLE, "ENABLE_AI")
    return state


def make_df(n: int = 300) -> pd.DataFrame:
    rng = __import__("numpy").random.default_rng(7)
    close = 1.08 + rng.normal(0, 0.0002, n).cumsum()
    df = pd.DataFrame({
        "time": pd.date_range("2026-10-01", periods=n, freq="15min"),
        "open": close, "high": close + 0.0003, "low": close - 0.0003,
        "close": close, "tick_volume": rng.integers(100, 2000, n).astype(float),
    })
    return df


def make_signal():
    return SimpleNamespace(
        direction="BUY", price=1.0810, stop_loss=1.0800,
        take_profit=1.0830, is_actionable=True, confidence=80.0,
        blocks=[], reject_reason=None,
    )


def make_engine_fakes(journal):
    view = {"market_open": True, "stale": False,
            "bar_ts": STAMP, "trade_mode": 4,
            "bid": 1.0810, "ask": 1.0812}
    read_history = MagicMock(return_value=make_df())
    evaluate_strategy = MagicMock(return_value=make_signal())
    build_order_intent = MagicMock(return_value=(0.1, "", []))
    return view, read_history, evaluate_strategy, build_order_intent


def run_cycle(journal, hook, view, read_history, evaluate_strategy, build_order_intent):
    scanner = ScanCycle(
        symbols=["EURUSD"], interval_seconds=0, broker_offset_seconds=0,
        kronos_hook=hook,
    )
    outcome = scanner.run(
        state=make_state(journal),
        journal=journal,
        read_market=lambda symbols: {"symbols": {"EURUSD": view}},
        mode="PAPER",
        read_history=read_history,
        evaluate_strategy=evaluate_strategy,
        build_order_intent=build_order_intent,
        utc_now_epoch=float(BAR_TS),
    )
    return outcome


def _pending_signals(journal):
    return journal.query(
        "SELECT * FROM signals WHERE status='PENDING' AND is_test=0")


def _kronos_events(journal):
    return journal.recent_events(limit=20, category="kronos")


def test_pending_signal_written_and_hook_called(tmp_path):
    journal = Journal(tmp_path / "k.db")
    hook = MagicMock()
    view, rh, es, boi = make_engine_fakes(journal)
    run_cycle(journal, hook, view, rh, es, boi)
    pend = _pending_signals(journal)
    assert len(pend) == 1, pend
    assert hook.called
    call_kw = hook.call_args.kwargs
    assert call_kw["direction"] == "BUY"
    assert call_kw["symbol"] == "EURUSD"
    assert call_kw["bar_ts"] is not None


def test_pending_signal_written_when_hook_raises(tmp_path):
    journal = Journal(tmp_path / "k.db")

    def boom(**kwargs):
        raise RuntimeError("hook exploded")

    view, rh, es, boi = make_engine_fakes(journal)
    outcome = run_cycle(journal, boom, view, rh, es, boi)
    pend = _pending_signals(journal)
    assert len(pend) == 1, pend
    events = _kronos_events(journal)
    assert any("hook raised" in e["message"] for e in events)


def test_pending_signal_written_when_hook_none(tmp_path):
    journal = Journal(tmp_path / "k.db")
    view, rh, es, boi = make_engine_fakes(journal)
    run_cycle(journal, None, view, rh, es, boi)
    assert len(_pending_signals(journal)) == 1


def test_observational_hook_journals_forecast(tmp_path):
    """The real hook, with a fake client, journals next to the signal."""
    from engine.research.kronos_hook import build_kronos_hook, journal_forecast
    journal = Journal(tmp_path / "k.db")

    class FakeClient:
        def forecast(self, symbol, bars, **kw):
            return {"direction": "UP", "agreement": 0.7,
                    "expected_move_pct": 0.05, "horizon_close_mean": 1.0815,
                    "last_close": 1.0810, "sample_count": 8, "pred_len": 8,
                    "symbol": symbol}

    cfg = SimpleNamespace(mode="PAPER", kronos=SimpleNamespace(
        enabled=True, endpoint="http://x", timeout_seconds=5,
        horizon_bars=8, sample_count=8, min_agreement=0.6,
        max_age_seconds=600))

    import engine.research.kronos_hook as kh
    orig = kh.KronosClient
    kh.KronosClient = lambda *a, **k: FakeClient()
    try:
        hook = build_kronos_hook(cfg, journal)
        assert hook is not None
        view, rh, es, boi = make_engine_fakes(journal)
        run_cycle(journal, hook, view, rh, es, boi)
    finally:
        kh.KronosClient = orig

    pend = _pending_signals(journal)
    assert len(pend) == 1
    events = _kronos_events(journal)
    ok = [e for e in events if json.loads(e["context_json"]).get("kind") == "kronos_ok"]
    assert ok, events
    ctx = json.loads(ok[0]["context_json"])
    assert ctx["forecast_direction"] == "UP"
    assert ctx["signal_direction"] == "BUY"
    assert ctx["model"].startswith("Kronos-small")
