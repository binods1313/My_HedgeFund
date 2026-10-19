"""Gateway history read: batched, closed-bar-only, 300-count.

The read_history method must:
  1. Run on the owner thread (via call()).
  2. Request exactly ``count`` closed bars (default 300) via
     copy_rates_from_pos with start_pos=1 (which skips the forming bar
     at index 0 in real MT5).
  3. Return None on empty feed (fail-closed).
  4. On the signal path, read history every eligible cycle. The closed bar's
     open time in that frame is the identity. The snapshot tick is not.

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


class TestScanCycleBarIdentity:
    """Signal-path identity is the history frame's last M15 open.

    The cycle reads history on every eligible pass. A repeat of the same
    open is a skip. A tick time is never stored. An unaligned frame is
    refused.
    """

    def test_empty_history_is_a_skip_and_is_read_again(self, tmp_path):
        """No frame means no closed bar. The next cycle reads again."""
        from engine.scan_cycle import ScanCycle
        from engine.state import STATE_IDLE, StateMachine

        journal = Journal(tmp_path / "scan.db")
        state = StateMachine(journal)
        state.transition(STATE_IDLE, "ENABLE_AI")
        history_reads: list[str] = []
        tick = "1760599062"  # 2026-10-16 05:17:42 UTC, not an M15 open

        def read_market(names):
            return {"symbols": {
                n: {"market_open": True, "stale": False, "bar_ts": tick,
                    "trade_mode": 4, "bid": 1.0900, "ask": 1.0902}
                for n in names
            }}

        def read_history(symbol):
            history_reads.append(symbol)
            return None

        def evaluate_strategy(symbol, df):
            return None

        scanner = ScanCycle(
            symbols=["EURUSD"], interval_seconds=0, remind_every=600,
            broker_offset_seconds=0,
        )
        out1 = scanner.run(
            state=state, journal=journal, mode="PAPER",
            read_market=read_market, read_history=read_history,
            evaluate_strategy=evaluate_strategy,
        )
        assert out1.scanned == 0
        assert out1.skipped == 1
        assert history_reads == ["EURUSD"]
        row = journal.query("SELECT bar_ts, reason FROM scan_symbols")[0]
        assert row["bar_ts"] is None
        assert row["reason"] == "NO_NEW_CLOSED_BAR"
        assert row["bar_ts"] != tick

        out2 = scanner.run(
            state=state, journal=journal, mode="PAPER",
            read_market=read_market, read_history=read_history,
            evaluate_strategy=evaluate_strategy,
        )
        assert out2.skipped == 1
        assert history_reads == ["EURUSD", "EURUSD"]
        journal.close()

    def test_frame_open_is_the_bar_and_an_unaligned_frame_is_refused(self, tmp_path):
        """The stored bar is the frame open. range() times are not M15 opens."""
        from datetime import datetime, timezone

        import pandas as pd

        from engine.scan_cycle import ScanCycle
        from engine.state import STATE_IDLE, StateMachine

        journal = Journal(tmp_path / "scan.db")
        state = StateMachine(journal)
        state.transition(STATE_IDLE, "ENABLE_AI")

        open_epoch = int(datetime(2026, 10, 16, 14, 45, tzinfo=timezone.utc).timestamp())
        assert open_epoch % 900 == 0
        tick = str(open_epoch + 17 * 60 + 42)
        assert int(tick) % 900 != 0
        history_reads = {"count": 0, "last": open_epoch}

        def _frame(last: int, n: int = 30) -> pd.DataFrame:
            times = [last - 900 * (n - 1 - i) for i in range(n)]
            return pd.DataFrame({
                "time": times,
                "open": [1.0] * n,
                "high": [1.01] * n,
                "low": [0.99] * n,
                "close": [1.0] * n,
                "tick_volume": [100] * n,
            })

        def read_market(names):
            return {"symbols": {
                n: {"market_open": True, "stale": False, "bar_ts": tick,
                    "trade_mode": 4, "bid": 1.0900, "ask": 1.0902}
                for n in names
            }}

        def read_history(symbol):
            history_reads["count"] += 1
            last = history_reads["last"]
            if last == "unaligned":
                return pd.DataFrame({
                    "time": list(range(30)),
                    "open": [1.0] * 30,
                    "high": [1.01] * 30,
                    "low": [0.99] * 30,
                    "close": [1.0] * 30,
                    "tick_volume": [100] * 30,
                })
            return _frame(int(last))

        def evaluate_strategy(symbol, df):
            return None

        scanner = ScanCycle(
            symbols=["EURUSD"], interval_seconds=0, remind_every=600,
            broker_offset_seconds=0,
        )
        first = scanner.run(
            state=state, journal=journal, mode="PAPER",
            read_market=read_market, read_history=read_history,
            evaluate_strategy=evaluate_strategy,
        )
        assert first.scanned == 1
        assert history_reads["count"] == 1
        stored = journal.query("SELECT bar_ts FROM scan_symbols ORDER BY id DESC")[0]
        assert int(stored["bar_ts"]) == open_epoch
        assert stored["bar_ts"] != tick

        second = scanner.run(
            state=state, journal=journal, mode="PAPER",
            read_market=read_market, read_history=read_history,
            evaluate_strategy=evaluate_strategy,
        )
        assert second.skipped == 1
        assert history_reads["count"] == 2
        repeat = journal.query(
            "SELECT bar_ts, reason FROM scan_symbols ORDER BY id DESC")[0]
        assert int(repeat["bar_ts"]) == open_epoch
        assert repeat["reason"] == "NO_NEW_CLOSED_BAR"

        history_reads["last"] = open_epoch + 900
        third = scanner.run(
            state=state, journal=journal, mode="PAPER",
            read_market=read_market, read_history=read_history,
            evaluate_strategy=evaluate_strategy,
        )
        assert third.scanned == 1
        advanced = journal.query("SELECT bar_ts FROM scan_symbols ORDER BY id DESC")[0]
        assert int(advanced["bar_ts"]) == open_epoch + 900

        history_reads["last"] = "unaligned"
        fourth = scanner.run(
            state=state, journal=journal, mode="PAPER",
            read_market=read_market, read_history=read_history,
            evaluate_strategy=evaluate_strategy,
        )
        assert fourth.scanned == 0
        refused = journal.query(
            "SELECT bar_ts, reason FROM scan_symbols ORDER BY id DESC")[0]
        assert refused["bar_ts"] is None
        assert refused["reason"] == "BAR_TIME_NOT_ALIGNED"
        assert journal.query("SELECT COUNT(*) AS n FROM signals")[0]["n"] == 0
        journal.close()
