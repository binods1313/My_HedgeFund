"""Gateway tests.

The behaviours under test are the ones that exist because of specific failure
modes:

  - A timeout must raise, never return None.
  - A timed-out order must be reconciled, never blindly retried. Retrying is
    how duplicate positions appear.
  - An UNKNOWN outcome must halt and raise a high-priority alert, because it
    needs manual intervention.
  - Disconnection must block trading and back off exponentially.

No real terminal: FakeMT5 is injected.

Run: python -m pytest tests/engine/test_gateway.py -v
"""
import time

import pytest

from engine.gateway import (
    DEFAULT_BACKOFF,
    Gateway,
    GatewayError,
    MT5Timeout,
    NotConnected,
    OrderOutcome,
    Resolution,
)
from engine.journal import Journal
from tests.test_engine.fake_mt5 import (
    BEHAVIOUR_HANG,
    BEHAVIOUR_INITIALIZE_FAILS,
    BEHAVIOUR_LOGIN_FAILS,
    BEHAVIOUR_HANG_THEN_RECONCILE_FAILS,
    BEHAVIOUR_REJECT,
    BEHAVIOUR_RETURN_NONE,
    BEHAVIOUR_TIMEOUT_NO_TRACE,
    BEHAVIOUR_TIMEOUT_THEN_EXECUTE,
    FakeMT5,
    FakeOrder,
    FakePosition,
)

SHORT_TIMEOUT = 0.4


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "engine.db")
    yield j
    j.close()


def make_gateway(mt5, journal, **kwargs):
    """Build and start a gateway around a FakeMT5.

    Credentials are supplied so login() runs, matching a real session. The
    password is a synthetic value: nothing here reaches a terminal.
    """
    creds = dict(login=999999999, pw="synthetic-test-value",
                 server="MetaQuotes-Demo")
    params = dict(call_timeout=SHORT_TIMEOUT, backoff=(1, 2, 4),
                  login=creds["login"], password=creds["pw"],
                  server=creds["server"],
                  mt5_module=mt5, journal=journal)
    params.update(kwargs)
    gw = Gateway(**params)
    gw.start()
    # Ask the OWNER THREAD to connect. Calling _ensure_connected() from here
    # would run initialize() on this thread, defeating the single-thread design.
    gw.ensure_connected()
    return gw


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Connection
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestConnection:

    def test_initialises_and_connects(self, journal):
        gw = make_gateway(FakeMT5(), journal)
        try:
            assert gw.connected is True
            assert "initialize" in gw._mt5.call_names()
            assert "login" in gw._mt5.call_names()
        finally:
            gw.stop()

    def test_initialise_runs_inside_the_owner_thread(self, journal):
        """initialize/login must not run on the caller's thread.

        MT5's API is not thread-safe; initialising from another thread has
        produced intermittent failures, so the owner thread does it.
        """
        mt5 = FakeMT5()
        seen: dict[str, int] = {}
        original_init = mt5.initialize

        def recording_init(**kwargs):
            seen["init_thread"] = threading_ident()
            return original_init(**kwargs)

        mt5.initialize = recording_init
        gw = make_gateway(mt5, journal)
        try:
            assert seen.get("init_thread") == gw._thread.ident, (
                "initialize() must run on the gateway owner thread"
            )
        finally:
            gw.stop()

    def test_calls_run_on_the_owner_thread(self, journal):
        mt5 = FakeMT5()
        seen: dict[str, int] = {}
        original = mt5.account_info

        def recording():
            seen["call_thread"] = threading_ident()
            return original()

        mt5.account_info = recording
        gw = make_gateway(mt5, journal)
        try:
            gw.account_info()
            assert seen["call_thread"] == gw._thread.ident, (
                "every MT5 call must run on the owner thread"
            )
        finally:
            gw.stop()

    def test_connect_failure_leaves_disconnected(self, journal):
        mt5 = FakeMT5(behaviour=BEHAVIOUR_INITIALIZE_FAILS, fail_times=5)
        gw = make_gateway(mt5, journal)
        try:
            assert gw.connected is False
            assert gw.status.last_error
        finally:
            gw.stop()

    def test_login_failure_disconnects(self, journal):
        mt5 = FakeMT5(behaviour=BEHAVIOUR_LOGIN_FAILS)
        gw = make_gateway(mt5, journal)
        try:
            assert gw.connected is False
            assert "login" in mt5.call_names(), (
                "a failed login must have been attempted"
            )
        finally:
            gw.stop()

    def test_backoff_is_monotonic_and_capped(self, journal):
        gw = Gateway(call_timeout=SHORT_TIMEOUT, backoff=(1, 2, 4, 8, 16, 30),
                     mt5_module=FakeMT5(), journal=journal)
        try:
            delays = []
            for _ in range(8):
                gw.status.consecutive_failures += 1
                gw._schedule_backoff()
                delays.append(gw.status.backoff_seconds)
            # Reaches the cap and stays there.
            assert delays[:6] == [1, 2, 4, 8, 16, 30]
            assert all(d == 30 for d in delays[5:])
            assert delays == sorted(delays), "backoff must never decrease"
        finally:
            gw.stop()

    def test_disconnected_gateway_refuses_to_send(self, journal):
        """Blocking while disconnected is the point of the connected flag."""
        mt5 = FakeMT5(behaviour=BEHAVIOUR_INITIALIZE_FAILS, fail_times=99)
        gw = make_gateway(mt5, journal)
        try:
            result = gw.send_order({"symbol": "EURUSD", "volume": 0.01,
                                    "type": "BUY"})
            assert result.ok is False
            assert result.outcome is OrderOutcome.NOT_EXECUTED
            assert "order_send" not in mt5.call_names(), (
                "order_send must not be attempted while disconnected"
            )
        finally:
            gw.stop()

    def test_call_while_disconnected_raises(self, journal):
        gw = Gateway(call_timeout=SHORT_TIMEOUT, mt5_module=FakeMT5(),
                     journal=journal)
        try:
            assert gw.connected is False
            with pytest.raises(NotConnected):
                gw.account_info()
        finally:
            gw.stop()


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Timeouts
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestTimeoutRaises:

    def test_raw_call_timeout_raises(self, journal):
        """The low-level path raises; send_order reconciles on top of it."""
        mt5 = FakeMT5(behaviour=BEHAVIOUR_HANG)
        gw = make_gateway(mt5, journal)
        try:
            with pytest.raises(MT5Timeout):
                # A bare callable that blocks: exactly the shape a slow MT5
                # call takes. This is the primitive that must raise rather than
                # return None.
                gw._submit("hanging_call", mt5.order_send, {},
                           timeout=SHORT_TIMEOUT)
        finally:
            gw.stop()

    def test_mt5timeout_is_a_timeouterror(self, journal):
        """Phase 0 code catches TimeoutError; that must keep working."""
        assert issubclass(MT5Timeout, TimeoutError)

    def test_send_order_reconciles_rather_than_propagating_timeout(self, journal):
        """send_order catches the timeout so it can reconcile.

        It must not surface a bare exception to the caller, because the caller
        cannot tell from one whether the order went through.
        """
        mt5 = FakeMT5(behaviour=BEHAVIOUR_TIMEOUT_NO_TRACE)
        gw = make_gateway(mt5, journal)
        try:
            result = gw.send_order({"symbol": "EURUSD", "volume": 0.01,
                                    "type": "BUY", "magic": 100100})
            assert isinstance(result, type(result))
            assert result is not None, "a timeout must not resolve to None"
            assert result.outcome is OrderOutcome.NOT_EXECUTED
            assert result.should_retry is True
        finally:
            gw.stop()

    def test_timeout_never_returns_none(self, journal):
        """A silent None is indistinguishable from 'returned nothing'."""
        mt5 = FakeMT5(behaviour=BEHAVIOUR_TIMEOUT_NO_TRACE)
        gw = make_gateway(mt5, journal)
        try:
            result = gw.send_order({"symbol": "EURUSD", "volume": 0.01,
                                    "type": "BUY", "magic": 100100})
            assert result is not None
            assert result.outcome is not None
        finally:
            gw.stop()

    def test_timeout_is_recorded_in_the_event_log(self, journal):
        mt5 = FakeMT5(behaviour=BEHAVIOUR_TIMEOUT_NO_TRACE)
        gw = make_gateway(mt5, journal)
        try:
            gw.send_order({"symbol": "EURUSD", "volume": 0.01,
                           "type": "BUY", "magic": 100100})
            events = [e["message"] for e in journal.recent_events(50)]
            assert any("timed out" in m or "reconcil" in m.lower() for m in events)
        finally:
            gw.stop()


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Order reconciliation  (the ghost-order guard)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestOrderReconciliation:

    def test_successful_order_reports_executed(self, journal):
        mt5 = FakeMT5()
        gw = make_gateway(mt5, journal)
        try:
            result = gw.send_order({"symbol": "EURUSD", "volume": 0.01,
                                    "type": "BUY", "price": 1.1})
            assert result.outcome is OrderOutcome.EXECUTED
            assert result.resolution is Resolution.PROCEED
            assert result.ticket
        finally:
            gw.stop()

    def test_broker_rejection_is_safe_to_retry(self, journal):
        mt5 = FakeMT5(behaviour=BEHAVIOUR_REJECT)
        gw = make_gateway(mt5, journal)
        try:
            result = gw.send_order({"symbol": "EURUSD", "volume": 0.01,
                                    "type": "BUY", "price": 1.1})
            assert result.outcome is OrderOutcome.NOT_EXECUTED
            assert result.should_retry is True
            assert result.retcode == FakeMT5.TRADE_RETCODE_REJECT
        finally:
            gw.stop()

    def test_timeout_that_executed_is_recovered_not_resent(self, journal):
        """The ghost-order case.

        The order DID fill; the caller just never saw the reply. Retrying would
        open a second position. Reconciliation must find the fill and report
        EXECUTED without sending again.
        """
        mt5 = FakeMT5(behaviour=BEHAVIOUR_TIMEOUT_THEN_EXECUTE)
        gw = make_gateway(mt5, journal)
        try:
            result = gw.send_order({"symbol": "EURUSD", "volume": 0.01,
                                    "type": "BUY", "magic": 100100})
            # Give the simulated broker thread time to record the fill.
            time.sleep(2.0)

            assert result.outcome is OrderOutcome.EXECUTED, (
                "a timed-out order that actually filled must be recovered"
            )
            assert result.should_retry is False, (
                "retrying a recovered fill would duplicate the position"
            )
            sends = [n for n in mt5.call_names() if n == "order_send"]
            assert len(sends) == 1, (
                f"order_send called {len(sends)} times; a timeout must not "
                f"trigger an automatic resend"
            )
        finally:
            gw.stop()

    def test_timeout_with_no_trace_is_safe_to_retry(self, journal):
        mt5 = FakeMT5(behaviour=BEHAVIOUR_TIMEOUT_NO_TRACE)
        gw = make_gateway(mt5, journal)
        try:
            result = gw.send_order({"symbol": "EURUSD", "volume": 0.01,
                                    "type": "BUY", "magic": 100100})
            assert result.outcome is OrderOutcome.NOT_EXECUTED
            assert result.should_retry is True
        finally:
            gw.stop()

    def test_failed_reconciliation_yields_unknown(self, journal):
        """If we cannot tell, we must not guess.

        The order timed out AND the reads needed to reconcile also failed. The
        only honest answer is UNKNOWN with HALT, because both 'executed' and
        'not executed' remain possible and either assumption could create or
        lose a position.
        """
        mt5 = FakeMT5(behaviour=BEHAVIOUR_HANG_THEN_RECONCILE_FAILS)
        gw = make_gateway(mt5, journal)
        try:
            result = gw.send_order({"symbol": "EURUSD", "volume": 0.01,
                                    "type": "BUY", "magic": 100100})

            assert result.outcome is OrderOutcome.UNKNOWN, (
                f"expected UNKNOWN when reconciliation fails, got {result.outcome}"
            )
            assert result.resolution is Resolution.HALT
            assert result.must_halt is True
            assert result.should_retry is False, (
                "an UNKNOWN outcome must never be retried automatically"
            )
        finally:
            gw.stop()

    def test_order_send_returning_none_is_reconciled(self, journal):
        """MT5 returning None is not proof of failure."""
        mt5 = FakeMT5(behaviour=BEHAVIOUR_RETURN_NONE)
        gw = make_gateway(mt5, journal)
        try:
            result = gw.send_order({"symbol": "EURUSD", "volume": 0.01,
                                    "type": "BUY", "magic": 100100})
            # No trace in positions or history, so a retry is safe.
            assert result.outcome is OrderOutcome.NOT_EXECUTED
            assert result.should_retry is True
        finally:
            gw.stop()

    def test_reconciliation_uses_our_magic_number_only(self, journal):
        """A position belonging to another EA must not be adopted as ours."""
        mt5 = FakeMT5(
            behaviour=BEHAVIOUR_TIMEOUT_NO_TRACE,
            positions=[FakePosition(ticket=555_001, magic=999_999)],
        )
        gw = make_gateway(mt5, journal)
        try:
            result = gw.send_order({"symbol": "EURUSD", "volume": 0.01,
                                    "type": "BUY", "magic": 100100})
            assert result.outcome is OrderOutcome.NOT_EXECUTED, (
                "another EA's position must not be treated as our fill"
            )
        finally:
            gw.stop()

    def test_reconciliation_uses_history_when_position_is_gone(self, journal):
        mt5 = FakeMT5(
            behaviour=BEHAVIOUR_TIMEOUT_NO_TRACE,
            history=[FakeOrder(ticket=555_002, magic=100100)],
        )
        gw = make_gateway(mt5, journal)
        try:
            result = gw.send_order({"symbol": "EURUSD", "volume": 0.01,
                                    "type": "BUY", "magic": 100100})
            assert result.outcome is OrderOutcome.EXECUTED
            assert result.comment and "history" in result.comment.lower()
        finally:
            gw.stop()


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Alerting on halt  (item 1.2)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestHaltAlerting:

    def test_unknown_outcome_writes_an_error_event(self, journal):
        """A halt needs manual intervention, so it must be loud in the log."""
        mt5 = FakeMT5(behaviour=BEHAVIOUR_HANG_THEN_RECONCILE_FAILS)
        gw = make_gateway(mt5, journal)
        try:
            result = gw.send_order({"symbol": "EURUSD", "volume": 0.01,
                                    "type": "BUY", "magic": 100100})
            assert result.must_halt, "precondition: this path must halt"

            errors = [e for e in journal.recent_events(100)
                      if e["severity"] == "ERROR"]
            assert errors, (
                "an UNKNOWN order outcome must produce a high-priority event; "
                f"events seen: {[(e['severity'], e['message']) for e in journal.recent_events(100)]}"
            )
        finally:
            gw.stop()

    def test_halt_event_names_the_symbol_and_the_problem(self, journal):
        """The alert has to be actionable, not just present."""
        mt5 = FakeMT5(behaviour=BEHAVIOUR_HANG_THEN_RECONCILE_FAILS)
        gw = make_gateway(mt5, journal)
        try:
            gw.send_order({"symbol": "EURUSD", "volume": 0.01,
                           "type": "BUY", "magic": 100100})
            errors = [e for e in journal.recent_events(100)
                      if e["severity"] == "ERROR"]
            assert errors
            import json
            contexts = " ".join(str(e.get("context_json", "")) for e in errors)
            assert "UNKNOWN" in " ".join(e["message"] for e in errors)
            assert "disconnected" in contexts.lower(), (
                f"the alert must include the underlying error; got {contexts}"
            )
        finally:
            gw.stop()

    def test_halt_event_is_written_before_returning(self, journal):
        """The alert must land before the caller acts on the result."""
        mt5 = FakeMT5(behaviour=BEHAVIOUR_HANG_THEN_RECONCILE_FAILS)
        gw = make_gateway(mt5, journal)
        try:
            result = gw.send_order({"symbol": "EURUSD", "volume": 0.01,
                                    "type": "BUY", "magic": 100100})
            assert result.must_halt
            errors = [e for e in journal.recent_events(50)
                      if e["severity"] == "ERROR"]
            assert errors, "halt result returned without an error event"
        finally:
            gw.stop()

    def test_timeout_that_needs_reconciliation_logs_a_warning(self, journal):
        mt5 = FakeMT5(behaviour=BEHAVIOUR_TIMEOUT_NO_TRACE)
        gw = make_gateway(mt5, journal)
        try:
            gw.send_order({"symbol": "EURUSD", "volume": 0.01,
                           "type": "BUY", "magic": 100100})
            events = journal.recent_events(50)
            assert any("timed out" in e["message"] or "reconcil" in e["message"].lower()
                       for e in events), (
                "a timed-out order must be logged before reconciliation"
            )
        finally:
            gw.stop()

    def test_reconnect_failure_logs_a_warning(self, journal):
        mt5 = FakeMT5(behaviour=BEHAVIOUR_INITIALIZE_FAILS, fail_times=3)
        gw = make_gateway(mt5, journal)
        try:
            gw.ensure_connected()
            warnings = [e for e in journal.recent_events(50)
                        if e["severity"] == "WARNING"]
            assert warnings, "a failed reconnect must be logged"
        finally:
            gw.stop()


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Close path
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestClosePosition:

    def test_close_of_unknown_position_is_not_an_error(self, journal):
        mt5 = FakeMT5()
        gw = make_gateway(mt5, journal)
        try:
            result = gw.close_position(123_456)
            assert result.ok is False
            assert "gone" in result.comment
        finally:
            gw.stop()

    def test_close_sends_an_order_for_an_open_position(self, journal):
        mt5 = FakeMT5(positions=[FakePosition(ticket=555_010)])
        gw = make_gateway(mt5, journal)
        try:
            result = gw.close_position(555_010)
            assert result.ok is True
            assert "order_send" in mt5.call_names()
        finally:
            gw.stop()


def threading_ident() -> int:
    import threading
    return threading.get_ident()
