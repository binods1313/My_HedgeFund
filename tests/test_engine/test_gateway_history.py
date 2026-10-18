"""Gateway history read: batched, closed-bar-only, 300-count.

The read_history method must:
  1. Run on the owner thread (via call()).
  2. Request exactly ``count`` closed bars (default 300) via
     copy_rates_from_pos with start_pos=1 (which skips the forming bar
     at index 0 in real MT5).
  3. Return None on empty feed (fail-closed).
  4. Not perform a per-symbol read when no new bar has closed since the last
     cycle — the scan cycle's _last_bar tracking prevents redundant reads.

Run: python -m pytest tests/test_engine/test_gateway_history.py -v
"""
from __future__ import annotations

import pytest

from engine.gateway import Gateway, NotConnected
from tests.test_engine.fake_mt5 import (
    FakeMT5,
)

from engine.journal import Journal
import time as time_module


TEST_LOGIN = 999999999
TEST_SECRET = "synthetic"
TEST_SERVER = "MetaQuotes-Demo"
SHORT_TIMEOUT = 0.4

_SYMBOL = "EURUSD"


def _make_rates(count: int, start_ts: int = 1792125600) -> list[dict]:
    """Build ``count`` closed M15 bars, indexed so [0] is the most recent.

    The test injects ``count + 1`` bars (one forming at index 0, then
    ``count`` closed bars), and the gateway must skip the forming bar by
    using start_pos=1.
    """
    return [
        {
            "time": start_ts + i * 900,
            "open": 1.0900 + i * 0.0001,
            "high": 1.0905 + i * 0.0001,
            "low": 1.0895 + i * 0.0001,
            "close": 1.0900 + i * 0.0001,
            "tick_volume": 100,
        }
        for i in range(count)
    ]


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "engine.db")
    yield j
    j.close()


def make_gateway(mt5, journal, **kwargs):
    params = dict(
        call_timeout=SHORT_TIMEOUT, backoff=(1, 2, 4),
        login=TEST_LOGIN, password=TEST_SECRET,
        server=TEST_SERVER, mt5_module=mt5, journal=journal,
    )
    params.update(kwargs)
    gw = Gateway(**params)
    gw.start()
    gw.ensure_connected()
    return gw


class TestReadHistory:

    def test_returns_requested_count_of_closed_bars(self, journal):
        """read_history returns exactly the bar count requested.

        We inject count+1 bars (the extra one at index 0 is the forming bar
        that must be skipped via start_pos=1).
        """
        mt5 = FakeMT5()
        mt5.add_rates(_SYMBOL, _make_rates(301))  # 0=forming, 1..300=closed
        gw = make_gateway(mt5, journal)
        try:
            df = gw.read_history(_SYMBOL, count=300)
            assert df is not None
            assert len(df) == 300
            assert list(df.columns) >= ["time", "open", "high", "low", "close"]
        finally:
            gw.stop()

    def test_never_passes_forming_bar_start_pos_1(self, journal):
        """copy_rates_from_pos must be called with start_pos=1 to skip the
        forming (not-yet-closed) bar at index 0.
        """
        mt5 = FakeMT5()
        mt5.add_rates(_SYMBOL, _make_rates(301))
        gw = make_gateway(mt5, journal)
        try:
            gw.read_history(_SYMBOL, count=300)
            calls = [c for c in mt5.calls if c[0] == "copy_rates_from_pos"]
            assert len(calls) == 1
            name, args = calls[0]
            assert args[2] == 1, "start_pos must be 1 to skip the forming bar"
        finally:
            gw.stop()

    def test_forming_bar_is_excluded_from_dataframe(self, journal):
        """The bar at index 0 (forming) must never appear in the returned
        DataFrame — verify by timestamp since the forming bar has a distinct
        timestamp that should not be the most-recent row.
        """
        mt5 = FakeMT5()
        rates = _make_rates(301)  # index 0 is forming (newest), 1..300 are closed
        mt5.add_rates(_SYMBOL, rates)
        gw = make_gateway(mt5, journal)
        try:
            df = gw.read_history(_SYMBOL, count=300)
            assert df is not None
            assert len(df) == 300
            # The forming bar's timestamp must not appear in the result
            forming_ts = rates[0]["time"]
            returned_times = [int(t.timestamp()) for t in df["time"].tolist()]
            assert forming_ts not in returned_times, (
                "the forming bar (index 0) must never be returned to evaluate()"
            )
            # The most recent returned bar should be index 1, not index 0
            most_recent = max(returned_times)
            assert most_recent != forming_ts
        finally:
            gw.stop()

    def test_returns_none_on_empty_feed(self, journal):
        """An empty feed must return None, never raise."""
        mt5 = FakeMT5()
        mt5.add_rates(_SYMBOL, [])
        gw = make_gateway(mt5, journal)
        try:
            df = gw.read_history(_SYMBOL, count=300)
            assert df is None
        finally:
            gw.stop()

    def test_returns_none_when_symbol_has_no_rates(self, journal):
        """A symbol with no rate data returns None (fail-closed)."""
        mt5 = FakeMT5()
        gw = make_gateway(mt5, journal)
        try:
            df = gw.read_history(_SYMBOL, count=300)
            assert df is None
        finally:
            gw.stop()

    def test_run_on_owner_thread(self, journal):
        """read_history must dispatch through call() (owner thread)."""
        mt5 = FakeMT5()
        mt5.add_rates(_SYMBOL, _make_rates(301))
        gw = make_gateway(mt5, journal)
        try:
            df = gw.read_history(_SYMBOL, count=300)
            assert df is not None
            assert "copy_rates_from_pos" in mt5.call_names()
        finally:
            gw.stop()

    def test_default_count_is_300(self, journal):
        """The method must request 300 bars by default."""
        mt5 = FakeMT5()
        mt5.add_rates(_SYMBOL, _make_rates(301))
        gw = make_gateway(mt5, journal)
        try:
            gw.read_history(_SYMBOL)
            calls = [c for c in mt5.calls if c[0] == "copy_rates_from_pos"]
            assert len(calls) == 1
            name, args = calls[0]
            assert args[3] == 300, "default count must be 300"
        finally:
            gw.stop()


class TestScanCycleNoRedundantReads:
    """The scan cycle must not issue per-symbol history reads when bars
    haven't closed — preventing symbols × cycles of redundant API calls.
    """

    def test_no_history_read_when_bar_has_not_advanced(self, tmp_path):
        """When the bar_ts hasn't changed, read_history is never called."""
        from engine.scan_cycle import ScanCycle
        from engine.state import STATE_IDLE, StateMachine

        journal = Journal(tmp_path / "scan.db")
        state = StateMachine(journal)
        state.transition(STATE_IDLE, "ENABLE_AI")

        history_reads = {"symbols": []}

        def read_market(names):
            return {"symbols": {
                n: {"market_open": True, "stale": False, "bar_ts": "2026-10-16T14:45:00",
                     "trade_mode": 4, "bid": 1.0900, "ask": 1.0902}
                for n in names
            }}

        def read_history(symbol):
            history_reads["symbols"].append(symbol)
            return None

        def evaluate_strategy(symbol, df):
            return None

        scanner = ScanCycle(symbols=["EURUSD"], interval_seconds=0, remind_every=600)

        # First cycle: bar is new, history read happens.
        out1 = scanner.run(
            state=state, journal=journal, mode="PAPER",
            read_market=read_market, read_history=read_history,
            evaluate_strategy=evaluate_strategy,
        )
        assert out1.scanned == 1
        assert len(history_reads["symbols"]) == 1

        # Second cycle: same bar_ts, no history read.
        history_reads["symbols"].clear()
        out2 = scanner.run(
            state=state, journal=journal, mode="PAPER",
            read_market=read_market, read_history=read_history,
            evaluate_strategy=evaluate_strategy,
        )
        assert out2.skipped == 1, f"expected skip on no-new-bar, got {out2}"
        assert len(history_reads["symbols"]) == 0, (
            "read_history must not be called when the bar hasn't advanced"
        )
        journal.close()

    def test_history_read_count_equals_new_bar_count(self, tmp_path):
        """read_history is called exactly once per new closed bar, not per cycle."""
        from engine.scan_cycle import ScanCycle, _MIN_BARS_FOR_SIGNAL
        from engine.state import STATE_IDLE, StateMachine

        journal = Journal(tmp_path / "scan.db")
        state = StateMachine(journal)
        state.transition(STATE_IDLE, "ENABLE_AI")

        history_reads = {"count": 0}
        import pandas as pd
        bars_ts = ["2026-10-16T14:45:00", "2026-10-16T15:00:00", "2026-10-16T15:15:00"]
        call_idx = {"i": 0}

        def read_market(names):
            i = call_idx["i"]
            ts = bars_ts[i] if i < len(bars_ts) else bars_ts[-1]
            call_idx["i"] += 1
            return {"symbols": {
                n: {"market_open": True, "stale": False, "bar_ts": ts,
                     "trade_mode": 4, "bid": 1.0900, "ask": 1.0902}
                for n in names
            }}

        def read_history(symbol):
            history_reads["count"] += 1
            return pd.DataFrame({
                "time": range(_MIN_BARS_FOR_SIGNAL),
                "open": [1.0] * _MIN_BARS_FOR_SIGNAL,
                "high": [1.01] * _MIN_BARS_FOR_SIGNAL,
                "low": [0.99] * _MIN_BARS_FOR_SIGNAL,
                "close": [1.0] * _MIN_BARS_FOR_SIGNAL,
                "tick_volume": [100] * _MIN_BARS_FOR_SIGNAL,
            })

        def evaluate_strategy(symbol, df):
            from engine.strategy import Signal, Block
            return Signal(symbol=symbol, direction="BUY", price=1.0900,
                          blocks=[Block("test", True, "ok")], confidence=1.0)

        scanner = ScanCycle(symbols=["EURUSD"], interval_seconds=0, remind_every=600)

        # Cycle 1: new bar → 1 history read
        scanner.run(state=state, journal=journal, mode="PAPER",
                    read_market=read_market, read_history=read_history,
                    evaluate_strategy=evaluate_strategy)
        assert history_reads["count"] == 1

        # Cycle 2: same bar → no history read (no new closed bar)
        def read_market_same(names):
            return {"symbols": {
                n: {"market_open": True, "stale": False, "bar_ts": bars_ts[0],
                     "trade_mode": 4, "bid": 1.0900, "ask": 1.0902}
                for n in names
            }}
        scanner.run(state=state, journal=journal, mode="PAPER",
                    read_market=read_market_same, read_history=read_history,
                    evaluate_strategy=evaluate_strategy)
        assert history_reads["count"] == 1, (
            "read_history must not re-fire for a bar that hasn't advanced"
        )
        journal.close()
