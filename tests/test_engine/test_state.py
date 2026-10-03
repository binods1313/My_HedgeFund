"""State machine tests, with emphasis on durability (item 1.1).

Two properties matter more than the transition table itself:

  - An illegal transition raises, and the journal is left intact and usable.
    A trading engine that corrupts its WAL while rejecting a bad transition
    loses its audit trail at exactly the moment it matters most.
  - EMERGENCY_STOP survives a restart. An engine that crashed mid-emergency and
    came back up trading again would be the worst possible failure mode.

Run: python -m pytest tests/test_engine/test_state.py -v
"""
import sqlite3

import pytest

from engine.journal import (
    STATE_EMERGENCY_STOP,
    STATE_IDLE,
    STATE_OFF,
    STATE_SCANNING,
    STATE_VALIDATING,
    Journal,
    SchemaTooNew,
)
from engine.state import (
    ALLOWED,
    IllegalTransition,
    StateMachine,
    allowed_from,
    is_allowed,
    triggers_from,
)


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "engine.db")
    yield j
    j.close()


@pytest.fixture
def sm(journal):
    return StateMachine(journal)


# ─────────────────────────────────────────────────────────────────────────────
# Declared transitions
# ─────────────────────────────────────────────────────────────────────────────

class TestTransitionTable:

    def test_starts_off(self, sm):
        assert sm.state == STATE_OFF

    def test_every_state_is_in_the_table(self):
        from engine.journal import ALL_STATES
        for state in ALL_STATES:
            assert state in ALLOWED, f"{state} has no declared transitions"

    def test_declared_transitions_are_accepted(self, sm, journal):
        """Walk the whole happy path; every step must be accepted."""
        sm.transition(STATE_IDLE, "ENABLE_AI")
        assert sm.state == STATE_IDLE

        sm.transition(STATE_SCANNING, "CYCLE_TICK")
        sm.transition("ANALYZING", "SCAN_COMPLETE")
        sm.transition(STATE_VALIDATING, "SIGNAL_PRODUCED")
        assert sm.state == STATE_VALIDATING

        sm.transition("EXECUTING", "RISK_APPROVED")
        sm.transition("MONITORING", "ORDER_SETTLED")
        sm.transition(STATE_IDLE, "CYCLE_COMPLETE")
        assert sm.state == STATE_IDLE

    @pytest.mark.parametrize("frm,to,trigger", [
        (STATE_OFF, STATE_IDLE, "ENABLE_AI"),
        (STATE_IDLE, STATE_OFF, "DISABLE_AI"),
        (STATE_IDLE, STATE_SCANNING, "CYCLE_TICK"),
        (STATE_IDLE, STATE_PAUSED_SAFE := "PAUSED", "PAUSE"),
        (STATE_IDLE, STATE_EMERGENCY_STOP, "EMERGENCY_STOP"),
        (STATE_VALIDATING, "EXECUTING", "RISK_APPROVED"),
        (STATE_VALIDATING, STATE_IDLE, "RISK_REJECTED"),
    ])
    def test_individual_allowed_transitions(self, sm, frm, to, trigger):
        sm.transition(STATE_IDLE, "ENABLE_AI")
        if frm == STATE_OFF:
            sm.reset_to_off()
            sm.transition(STATE_IDLE, "ENABLE_AI")
            sm.reset_to_off()
        elif frm != STATE_IDLE:
            sm._state = frm
        assert sm.can(to, trigger) is True
        assert sm.transition(to, trigger) == to

    def test_illegal_transition_raises(self, sm):
        sm.transition(STATE_IDLE, "ENABLE_AI")
        with pytest.raises(IllegalTransition):
            # IDLE has no path to EXECUTING; going there would place a trade
            # from a state the risk logic never evaluated.
            sm.transition("EXECUTING", "RISK_APPROVED")

    def test_illegal_transition_leaves_state_unchanged(self, sm):
        sm.transition(STATE_IDLE, "ENABLE_AI")
        with pytest.raises(IllegalTransition):
            sm.transition("EXECUTING", "RISK_APPROVED")
        assert sm.state == STATE_IDLE, "a rejected transition must not move state"

    def test_unknown_target_state_raises(self, sm):
        sm.transition(STATE_IDLE, "ENABLE_AI")
        with pytest.raises(IllegalTransition):
            sm.transition("WARP_SPEED", "ENABLE_AI")

    def test_wrong_trigger_for_a_valid_target_is_rejected(self, sm):
        """Reaching IDLE is legal, but not via an arbitrary trigger."""
        sm.transition(STATE_IDLE, "ENABLE_AI")
        sm.transition(STATE_SCANNING, "CYCLE_TICK")
        with pytest.raises(IllegalTransition):
            sm.transition(STATE_IDLE, "ENABLE_AI")

    def test_helper_functions_agree_with_the_table(self):
        assert STATE_IDLE in allowed_from(STATE_OFF)
        assert is_allowed(STATE_OFF, STATE_IDLE, "ENABLE_AI") is True
        assert is_allowed(STATE_OFF, STATE_IDLE, "PAUSE") is False
        assert "ENABLE_AI" in triggers_from(STATE_OFF)


class TestEmergencyStopIsTerminal:
    """Item: EMERGENCY_STOP must not be left automatically."""

    def test_emergency_reachable_from_operational_states(self, sm):
        for state in ("IDLE", "SCANNING", "ANALYZING", "VALIDATING",
                      "EXECUTING", "MONITORING", "PAUSED"):
            sm._state = state
            assert sm.can(STATE_EMERGENCY_STOP, "EMERGENCY_STOP"), (
                f"EMERGENCY_STOP must be reachable from {state}"
            )

    def test_emergency_is_not_reachable_from_off(self, sm):
        """An engine that is off has nothing to stop."""
        assert sm.can(STATE_EMERGENCY_STOP, "EMERGENCY_STOP") is False

    def test_nothing_leaves_emergency_except_reset(self, sm):
        """No timeout, no reconnect, no automatic path out."""
        assert list(allowed_from(STATE_EMERGENCY_STOP)) == [STATE_OFF], (
            "EMERGENCY_STOP must have exactly one exit: RESET_EMERGENCY to OFF"
        )
        assert triggers_from(STATE_EMERGENCY_STOP) == {"RESET_EMERGENCY"}

    def test_pause_and_cycle_cannot_leave_emergency(self, sm):
        sm.transition(STATE_IDLE, "ENABLE_AI")
        sm.transition(STATE_EMERGENCY_STOP, "EMERGENCY_STOP")
        for trigger in ("PAUSE", "RESUME", "CYCLE_TICK", "DISABLE_AI",
                        "SCAN_COMPLETE", "CYCLE_COMPLETE"):
            assert sm.can(STATE_IDLE, trigger) is False, (
                f"{trigger} must not be able to leave EMERGENCY_STOP"
            )

    def test_disabling_ai_does_not_escape_emergency(self, sm):
        """DISABLE_AI must not become a back door out of emergency."""
        sm.transition(STATE_IDLE, "ENABLE_AI")
        sm.transition(STATE_EMERGENCY_STOP, "EMERGENCY_STOP")
        ok, message = sm.apply("DISABLE_AI", {})
        assert ok is False, "DISABLE_AI must be refused while in EMERGENCY_STOP"
        assert sm.state == STATE_EMERGENCY_STOP


# ─────────────────────────────────────────────────────────────────────────────
# Item 1.1: durability
# ─────────────────────────────────────────────────────────────────────────────

class TestIllegalTransitionDoesNotCorruptTheJournal:

    def test_journal_still_queryable_after_illegal_transition(self, sm, journal):
        """The WAL must remain usable after a rejected transition."""
        sm.transition(STATE_IDLE, "ENABLE_AI")
        for _ in range(5):
            with pytest.raises(IllegalTransition):
                sm.transition("EXECUTING", "RISK_APPROVED")

        assert journal.get_state() is not None
        assert journal.get_state()["state"] == STATE_IDLE
        assert len(journal.recent_events(100)) > 0

    def test_journal_can_still_be_written_after_rejection(self, sm, journal):
        sm.transition(STATE_IDLE, "ENABLE_AI")
        with pytest.raises(IllegalTransition):
            sm.transition("EXECUTING", "RISK_APPROVED")

        event_id = journal.log_event("test", "still writable after rejection")
        assert event_id > 0
        row = journal.query_one("SELECT * FROM events WHERE id = ?", (event_id,))
        assert row is not None

    def test_rejection_is_not_recorded_as_a_transition(self, sm, journal):
        """A rejected move must not appear to have happened."""
        sm.transition(STATE_IDLE, "ENABLE_AI")
        before = len(journal.recent_events(200))
        with pytest.raises(IllegalTransition):
            sm.transition("EXECUTING", "RISK_APPROVED")
        after = len(journal.recent_events(200))
        assert after == before, (
            "an illegal transition must not write an events row"
        )

    def test_database_passes_integrity_check_after_rejection(self, sm, journal):
        sm.transition(STATE_IDLE, "ENABLE_AI")
        with pytest.raises(IllegalTransition):
            sm.transition("EXECUTING", "RISK_APPROVED")

        row = journal.query_one("PRAGMA integrity_check")
        assert row is not None
        assert row[0] == "ok", f"SQLite integrity check failed: {row[0]}"

    def test_wal_mode_still_active(self, sm, journal):
        """WAL is what allows the UI to read while the engine writes."""
        sm.transition(STATE_IDLE, "ENABLE_AI")
        with pytest.raises(IllegalTransition):
            sm.transition("EXECUTING", "RISK_APPROVED")
        mode = journal.query_one("PRAGMA journal_mode")[0]
        assert mode.lower() == "wal", f"expected WAL, got {mode}"


class TestEmergencyStopSurvivesRestart:
    """Item 1.1: a crash during an emergency must not resume trading."""

    def test_emergency_persists_across_a_restart(self, tmp_path):
        db = tmp_path / "engine.db"

        journal = Journal(db)
        sm = StateMachine(journal)
        sm.transition(STATE_IDLE, "ENABLE_AI")
        sm.transition(STATE_EMERGENCY_STOP, "EMERGENCY_STOP",
                      reason="crash test")
        # Simulate a crash: no clean shutdown, no transition out.
        journal.close()

        journal2 = Journal(db)
        sm2 = StateMachine(journal2)
        assert sm2.state == STATE_EMERGENCY_STOP, (
            f"a restart must resume EMERGENCY_STOP, got {sm2.state}"
        )
        journal2.close()

    def test_other_states_also_persist(self, tmp_path):
        db = tmp_path / "engine.db"

        journal = Journal(db)
        sm = StateMachine(journal)
        sm.transition(STATE_IDLE, "ENABLE_AI")
        journal.close()

        journal2 = Journal(db)
        sm2 = StateMachine(journal2)
        assert sm2.state == STATE_IDLE
        journal2.close()

    def test_restart_in_emergency_blocks_trading_paths(self, tmp_path):
        """Resuming must not quietly re-enable submissions."""
        db = tmp_path / "engine.db"

        journal = Journal(db)
        sm = StateMachine(journal)
        sm.transition(STATE_IDLE, "ENABLE_AI")
        sm.transition(STATE_EMERGENCY_STOP, "EMERGENCY_STOP")
        journal.close()

        journal2 = Journal(db)
        sm2 = StateMachine(journal2)
        assert sm2.state == STATE_EMERGENCY_STOP
        ok, _ = sm2.apply("ENABLE_AI", {})
        assert ok is False, "ENABLE_AI must be refused after an emergency restart"
        journal2.close()

    def test_off_persists_for_a_fresh_database(self, tmp_path):
        journal = Journal(tmp_path / "engine.db")
        assert StateMachine(journal).state == STATE_OFF
        journal.close()


class TestResetEmergency:

    def test_requires_the_exact_confirmation_phrase(self, sm):
        sm.transition(STATE_IDLE, "ENABLE_AI")
        sm.transition(STATE_EMERGENCY_STOP, "EMERGENCY_STOP")

        ok, message = sm.apply("RESET_EMERGENCY", {"confirmation": "nope"})
        assert ok is False
        assert sm.state == STATE_EMERGENCY_STOP, "a wrong phrase must not reset"

    def test_correct_phrase_resets_to_off(self, sm):
        sm.transition(STATE_IDLE, "ENABLE_AI")
        sm.transition(STATE_EMERGENCY_STOP, "EMERGENCY_STOP")

        ok, _ = sm.apply("RESET_EMERGENCY", {"confirmation": "RESET"})
        assert ok is True
        assert sm.state == STATE_OFF

    def test_missing_confirmation_is_refused(self, sm):
        sm.transition(STATE_IDLE, "ENABLE_AI")
        sm.transition(STATE_EMERGENCY_STOP, "EMERGENCY_STOP")
        ok, _ = sm.apply("RESET_EMERGENCY", {})
        assert ok is False
        assert sm.state == STATE_EMERGENCY_STOP

    def test_reset_only_applies_in_emergency(self, sm):
        ok, _ = sm.apply("RESET_EMERGENCY", {"confirmation": "RESET"})
        assert ok is False, "there is nothing to reset when not in emergency"


class TestStateLogging:

    def test_every_transition_is_logged(self, sm, journal):
        sm.transition(STATE_IDLE, "ENABLE_AI")
        sm.transition(STATE_SCANNING, "CYCLE_TICK")
        messages = [e["message"] for e in journal.recent_events(50, "state")]
        assert any("OFF -> IDLE" in m for m in messages)
        assert any("IDLE -> SCANNING" in m for m in messages)

    def test_transition_context_carries_trigger_and_reason(self, sm, journal):
        sm.transition(STATE_IDLE, "ENABLE_AI", reason="operator asked")
        import json
        row = next(e for e in journal.recent_events(50, "state")
                   if "-> IDLE" in e["message"])
        context = json.loads(row["context_json"])
        assert context["trigger"] == "ENABLE_AI"
        assert context["reason"] == "operator asked"
        assert context["from"] == STATE_OFF

    def test_callback_is_invoked_on_every_transition(self, journal):
        seen = []
        sm = StateMachine(journal, on_transition=lambda *a: seen.append(a))
        sm.transition(STATE_IDLE, "ENABLE_AI")
        sm.transition(STATE_SCANNING, "CYCLE_TICK")
        assert [s[1] for s in seen] == [STATE_IDLE, STATE_SCANNING]


class TestHeartbeat:

    def test_alive_after_a_heartbeat(self, sm):
        assert sm.is_alive() is False, "no heartbeat means not provably alive"
        sm.heartbeat(connected=True)
        assert sm.is_alive() is True

    def test_not_alive_without_any_heartbeat(self, sm):
        assert sm.heartbeat_age() is None
        assert sm.is_alive() is False

    def test_stale_heartbeat_is_not_alive(self, sm, journal):
        sm.heartbeat(connected=True)
        assert sm.is_alive() is True
        # Backdate the newest heartbeat beyond the threshold.
        journal.execute(
            "UPDATE heartbeats SET ts = '2000-01-01T00:00:00+00:00' WHERE id = "
            "(SELECT MAX(id) FROM heartbeats)")
        assert sm.is_alive(offline_after=15) is False

    def test_offline_threshold_is_configurable(self, sm):
        sm.heartbeat(connected=True)
        assert sm.is_alive(offline_after=3600) is True


class TestSchemaTooNew:
    """An engine must refuse a database written by a newer build."""

    def test_newer_schema_is_refused(self, tmp_path):
        db = tmp_path / "engine.db"
        journal = Journal(db)
        journal.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (999, ?)",
            ("2026-01-01T00:00:00+00:00",))
        journal.close()

        with pytest.raises(SchemaTooNew):
            Journal(db)