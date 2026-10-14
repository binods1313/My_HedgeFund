"""Engine telemetry tests.

Added after Phase 1, where the engine reported a clean startup while its
heartbeats recorded connected=0 on every beat. Nothing asserted on that column,
so a total loss of broker connectivity looked identical to a healthy engine.

The UI screenshot said CONNECTED; only the telemetry knew the engine was not.
Run: python -m pytest tests/test_engine/test_telemetry.py -v
"""
from pathlib import Path

import pytest

from engine.__main__ import build_engine
from engine.journal import Journal
from engine.state import StateMachine
from tests.test_engine.fake_mt5 import (
    matching_fake_login,
    BEHAVIOUR_INITIALIZE_FAILS,
    FakeMT5,
    FakePosition,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "telemetry.db")
    yield j
    j.close()


class TestHeartbeatRecordsConnectivity:

    def test_heartbeat_carries_the_connected_flag(self, journal):
        sm = StateMachine(journal)
        sm.heartbeat(connected=True)
        row = journal.last_heartbeat()
        assert row["connected"] == 1, (
            "a connected heartbeat must record connected=1; a silent "
            "connected=0 is what made the Phase 1 outage invisible"
        )

    def test_disconnected_heartbeat_is_recorded(self, journal):
        sm = StateMachine(journal)
        sm.heartbeat(connected=False)
        assert journal.last_heartbeat()["connected"] == 0

    def test_heartbeat_is_not_live_when_never_connected(self, journal):
        """Liveness and connectivity are different facts.

        A heartbeat that proves the process is alive does not prove the
        terminal is reachable. is_alive() must not imply connected.
        """
        sm = StateMachine(journal)
        sm.heartbeat(connected=False)
        assert sm.is_alive() is True, "the process is alive"
        row = journal.last_heartbeat()
        assert row["connected"] == 0, "but the terminal is not connected"

    def test_engine_writes_connected_true_after_startup(self, tmp_path):
        mt5 = FakeMT5(positions=[FakePosition(ticket=1, magic=100100)])
        engine = build_engine(root=REPO_ROOT,
                              journal_path=tmp_path / "hb.db",
                              mt5_module=mt5)
        try:
            engine.state.heartbeat(connected=engine.gateway.connected)
            row = engine.journal.last_heartbeat()
            assert row["connected"] == 1, (
                "after a successful connect the heartbeat must say so"
            )
        finally:
            engine.gateway.shutdown_terminal()
            engine.gateway.stop()
            engine.journal.close()

    def test_engine_records_disconnect_in_telemetry(self, tmp_path):
        """A gateway that never connects must be visible in the telemetry."""
        mt5 = FakeMT5(behaviour=BEHAVIOUR_INITIALIZE_FAILS, fail_times=99)
        engine = build_engine(root=REPO_ROOT,
                              journal_path=tmp_path / "hb2.db",
                              mt5_module=mt5)
        try:
            assert engine.gateway.connected is False
            engine.state.heartbeat(connected=engine.gateway.connected)
            row = engine.journal.last_heartbeat()
            assert row["connected"] == 0
            # And the failure must be explained, not merely recorded as 0.
            events = [e["message"] for e in engine.journal.recent_events(50)]
            assert any("connect" in m.lower() for m in events), (
                f"a failed connect must be logged; events: {events}"
            )
        finally:
            engine.gateway.stop()
            engine.journal.close()


class TestTelemetryGapsAreDetectable:
    """Regressions that would otherwise pass silently."""

    def test_zero_connected_heartbeats_are_distinguishable(self, tmp_path):
        """The Phase 1 failure mode, as a test.

        Every heartbeat recording connected=0 while the engine reported a
        healthy startup must be impossible to mistake for a good run.
        """
        mt5 = FakeMT5(behaviour=BEHAVIOUR_INITIALIZE_FAILS, fail_times=99)
        engine = build_engine(root=REPO_ROOT,
                              journal_path=tmp_path / "silent.db",
                              mt5_module=mt5)
        try:
            for _ in range(3):
                engine.state.heartbeat(connected=engine.gateway.connected)
            rows = engine.journal.query(
                "SELECT connected FROM heartbeats ORDER BY id")
            assert all(r["connected"] == 0 for r in rows)
            # The signal must be queryable, not only visible in a log line.
            connected_count = engine.journal.query_one(
                "SELECT COUNT(*) n FROM heartbeats WHERE connected = 1")["n"]
            assert connected_count == 0, (
                "a queryable count of connected heartbeats lets an operator "
                "or a UI panel assert on gateway health"
            )
        finally:
            engine.gateway.stop()
            engine.journal.close()

    def test_equity_snapshot_requires_a_connection(self, tmp_path):
        """No account data means no equity snapshot; never fabricate one."""
        mt5 = FakeMT5(behaviour=BEHAVIOUR_INITIALIZE_FAILS, fail_times=99)
        engine = build_engine(root=REPO_ROOT,
                              journal_path=tmp_path / "eq.db",
                              mt5_module=mt5)
        try:
            engine._write_telemetry()
            n = engine.journal.query_one(
                "SELECT COUNT(*) n FROM equity_snapshots")["n"]
            assert n == 0, (
                "an equity snapshot must not be written while disconnected; "
                "a fabricated snapshot would drive risk limits on fiction"
            )
        finally:
            engine.gateway.stop()
            engine.journal.close()

    def test_equity_snapshot_written_when_connected(self, tmp_path):
        mt5 = FakeMT5(positions=[FakePosition(ticket=1, magic=100100)])
        engine = build_engine(root=REPO_ROOT,
                              journal_path=tmp_path / "eq2.db",
                              mt5_module=mt5)
        try:
            engine._write_telemetry()
            row = engine.journal.query_one(
                "SELECT * FROM equity_snapshots ORDER BY id DESC LIMIT 1")
            assert row is not None, "a connected engine must record equity"
            assert row["equity"] is not None
        finally:
            engine.gateway.shutdown_terminal()
            engine.gateway.stop()
            engine.journal.close()


class ConnectedButEmptyMT5(FakeMT5):
    """Connected gateway whose account read yields nothing *after* startup.

    Models the live failure recorded between 2026-10-07 and 2026-10-10: the
    terminal was attached and heartbeating, but account_info() yielded None, so
    the engine ran for days while writing no equity at all.

    The first call must succeed, otherwise the account guard blocks telemetry
    at startup and this test would assert on the wrong gate. That is the
    guard's job and it is covered in test_account_guard.py; what this exercises
    is the gap that opens later, on a gateway the guard already verified.
    """

    def account_info(self):
        self._log("account_info")
        if getattr(self, "_account_calls", 0) >= 1:
            return None
        self._account_calls = 1
        return super().account_info()


class TestConnectedButAccountlessIsVisible:
    """A gateway that reports connected while the account read is empty."""

    def _engine(self, tmp_path, name):
        engine = build_engine(root=REPO_ROOT,
                              journal_path=tmp_path / name,
                              mt5_module=ConnectedButEmptyMT5())
        assert engine.gateway.connected is True
        return engine

    def test_no_equity_is_written_but_the_gap_is_journalled(self, tmp_path):
        engine = self._engine(tmp_path, "empty_account.db")
        try:
            engine._write_telemetry()
            n = engine.journal.query_one(
                "SELECT COUNT(*) n FROM equity_snapshots")["n"]
            assert n == 0, (
                "no equity snapshot may be fabricated from a missing account"
            )
            warnings = [e for e in engine.journal.recent_events(50)
                        if e["category"] == "telemetry"
                        and e["severity"] == "WARNING"]
            assert warnings, (
                "a connected engine that writes no equity must say so; the "
                "production gap produced heartbeats and silence for days"
            )
            assert any("account_info" in e["message"] for e in warnings), (
                "the warning must name the empty account read: "
                + str([e["message"] for e in warnings])
            )
        finally:
            engine.gateway.shutdown_terminal()
            engine.gateway.stop()
            engine.journal.close()

    def test_the_warning_is_repeated_bounded_not_silenced(self, tmp_path):
        """The gap repeats every loop; the journal must not flood."""
        engine = self._engine(tmp_path, "repeat_account.db")
        try:
            for _ in range(4):
                engine._write_telemetry()
            warnings = [e for e in engine.journal.recent_events(50)
                        if e["category"] == "telemetry"]
            assert len(warnings) == 1, (
                "four consecutive gaps in one run must log once, not four "
                f"times; got {len(warnings)}"
            )
            # The process is still alive and still failing, which the journal
            # shows through the heartbeats rather than a WARNING storm.
            engine.state.heartbeat(connected=engine.gateway.connected)
            assert engine.journal.last_heartbeat()["connected"] == 1
        finally:
            engine.gateway.shutdown_terminal()
            engine.gateway.stop()
            engine.journal.close()

    def test_a_raising_account_read_is_also_journalled(self, tmp_path):
        class RaisingMT5(FakeMT5):
            """Fails only after startup, so the guard has already verified."""

            def account_info(self):
                self._log("account_info")
                if getattr(self, "_account_calls", 0) >= 1:
                    raise RuntimeError("terminal gone mid-call")
                self._account_calls = 1
                return super().account_info()

        engine = build_engine(root=REPO_ROOT,
                              journal_path=tmp_path / "raising.db",
                              mt5_module=RaisingMT5())
        assert engine.gateway.connected is True
        try:
            engine._write_telemetry()   # must not raise
            n = engine.journal.query_one(
                "SELECT COUNT(*) n FROM equity_snapshots")["n"]
            assert n == 0
            warnings = [e for e in engine.journal.recent_events(50)
                        if e["category"] == "telemetry"]
            assert warnings, "an exception in the account read must be visible"
            assert any("raised" in e["message"] for e in warnings)
        finally:
            engine.gateway.shutdown_terminal()
            engine.gateway.stop()
            engine.journal.close()