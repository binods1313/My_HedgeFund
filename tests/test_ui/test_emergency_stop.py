"""Emergency stop UI tests.

The engine half is already proven elsewhere; these tests cover the dashboard
half: that a click enqueues the right command with the right parameters, that
the reset path carries the typed phrase, and that both render states behave.

Mocked journal only. No terminal, no broker, no orders.
Run: python -m pytest tests/test_ui/test_emergency_stop.py -v
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from engine.commands import CommandProcessor
from engine.journal import (
    MIGRATION_1,
    MIGRATION_2,
    STATE_EMERGENCY_STOP,
    Journal,
    utc_now_iso,
)
from engine.state import StateMachine
from ui.components.emergency_stop import (
    RESET_PHRASE,
    JournalFileUnavailable,
    emergency_stop_availability,
    queue_emergency_stop,
    queue_reset_emergency,
)


@pytest.fixture(autouse=True)
def _commands_table_exists(tmp_path):
    """The halt inserts into an existing commands table. It does not create one.

    Tests that pass ``tmp_path / 'engine.db'`` get a current journal here.
    Schema-2 and lock tests use their own filenames.
    """
    path = tmp_path / "engine.db"
    journal = Journal(path)
    journal.close()


def _command_rows(database: Path) -> list[dict]:
    if not database.exists():
        return []
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in
                    conn.execute("SELECT command, params_json FROM commands ORDER BY id")]
        except sqlite3.OperationalError:
            # A fresh database from a read that never wrote anything: the
            # table does not exist yet, which is the same as "no commands".
            return []


# ─────────────────────────────────────────────────────────────────────────────
# Queueing
# ─────────────────────────────────────────────────────────────────────────────

class TestQueueing:

    def test_a_click_enqueues_the_engine_command(self, tmp_path):
        database = tmp_path / "engine.db"
        queue_emergency_stop(database)
        rows = _command_rows(database)
        assert len(rows) == 1
        assert rows[0]["command"] == "EMERGENCY_STOP"
        params = json.loads(rows[0]["params_json"] or "{}")
        assert params["close_positions"] is True
        assert params["source"] == "ui"

    def test_positions_can_be_kept_open(self, tmp_path):
        """The operator may halt without sweeping; the flag must survive."""
        database = tmp_path / "engine.db"
        queue_emergency_stop(database, close_positions=False)
        params = json.loads(_command_rows(database)[0]["params_json"])
        assert params["close_positions"] is False

    def test_reset_carries_the_typed_phrase(self, tmp_path):
        database = tmp_path / "engine.db"
        queue_reset_emergency(database, confirmation=RESET_PHRASE)
        rows = _command_rows(database)
        assert rows[0]["command"] == "RESET_EMERGENCY"
        params = json.loads(rows[0]["params_json"])
        assert params["confirmation"] == "RESET"

    def test_a_wrong_phrase_is_still_queued_for_the_engine_to_reject(self, tmp_path):
        """Validation lives in the state machine, not the UI.

        The UI queues what was typed; the engine refuses it. Duplicating the
        rule here would create a second authority that could drift.
        """
        database = tmp_path / "engine.db"
        queue_reset_emergency(database, confirmation="go")
        rows = _command_rows(database)
        assert rows[0]["command"] == "RESET_EMERGENCY"
        assert json.loads(rows[0]["params_json"])["confirmation"] == "go"


# ─────────────────────────────────────────────────────────────────────────────
# End to end through the real command processor
# ─────────────────────────────────────────────────────────────────────────────

class TestEngineRoundTrip:

    def test_the_queued_command_really_halts_the_machine(self, tmp_path):
        database = tmp_path / "engine.db"
        journal = Journal(database)
        try:
            sm = StateMachine(journal)
            # The table deliberately refuses OFF -> EMERGENCY_STOP: an engine
            # that is off has nothing to halt. Arm first, like a live session.
            assert sm.apply("ENABLE_AI")[0] is True
            queue_emergency_stop(database)
            outcomes = CommandProcessor(journal, sm).poll_once()
            assert outcomes and outcomes[0][1].ok
            assert sm.state == STATE_EMERGENCY_STOP

            queue_reset_emergency(database, confirmation=RESET_PHRASE)
            outcomes = CommandProcessor(journal, sm).poll_once()
            assert outcomes and outcomes[0][1].ok
            assert sm.state == "OFF"
        finally:
            journal.close()

    def test_a_wrong_phrase_leaves_the_machine_halted(self, tmp_path):
        database = tmp_path / "engine.db"
        journal = Journal(database)
        try:
            sm = StateMachine(journal)
            assert sm.apply("ENABLE_AI")[0] is True
            queue_emergency_stop(database)
            CommandProcessor(journal, sm).poll_once()
            queue_reset_emergency(database, confirmation="reset please")
            outcomes = CommandProcessor(journal, sm).poll_once()
            assert outcomes and not outcomes[0][1].ok
            assert sm.state == STATE_EMERGENCY_STOP
        finally:
            journal.close()


# ─────────────────────────────────────────────────────────────────────────────
# Availability
# ─────────────────────────────────────────────────────────────────────────────

class TestAvailability:

    @pytest.mark.parametrize("mode", ["PAPER", "DEMO", "LIVE", "paper", "demo",
                                      "live", "", "UNKNOWN", None])
    def test_no_mode_ever_disables_the_halt(self, mode):
        """The halt is the last resort; the mode string must not gate it.

        A runaway scan loop has to be stoppable whatever the mode read says,
        including when that read is empty or unrecognised. Gating on the mode
        meant an operator watching an engine go wrong had no control at all.
        """
        enabled, reason = emergency_stop_availability(mode, "running")
        assert enabled is True
        assert reason == ""

    @pytest.mark.parametrize("mode", ["PAPER", "DEMO", "LIVE"])
    @pytest.mark.parametrize("engine_state", ["running", "paused", "idle",
                                              "scanning", "analyzing", None])
    def test_the_halt_is_offered_in_every_running_mode(self, mode, engine_state):
        enabled, _ = emergency_stop_availability(mode, engine_state)
        assert enabled is True

    def test_an_already_halted_engine_does_not_offer_a_second_stop(self):
        """Once halted, the reset control takes the button's place.

        This is the only state that disables it, and it disables it because
        the action is already done -- not because the mode forbids it.
        """
        for mode in ("PAPER", "DEMO", "LIVE"):
            enabled, reason = emergency_stop_availability(mode, "halted")
            assert enabled is False
            assert "already active" in reason

# ─────────────────────────────────────────────────────────────────────────────
# Rendering (Streamlit AppTest, mirroring test_task9)
# ─────────────────────────────────────────────────────────────────────────────

def _render_script(database: Path, mode: str = "PAPER", state: str = "running") -> str:
    return (
        "from ui.components.emergency_stop import render_emergency_stop\n"
        f"render_emergency_stop({mode!r}, {state!r}, db_path={str(database)!r})\n"
    )


@pytest.mark.parametrize("mode", ["PAPER", "DEMO", "LIVE"])
def test_the_button_is_live_in_every_mode(tmp_path, mode):
    """Rendered, not just computed: the control must appear in all three.

    The availability function is unit-tested above; this proves the wiring --
    that the mode reaches the button as a live, clickable control rather than
    being filtered out somewhere in between.
    """
    from streamlit.testing.v1 import AppTest

    database = tmp_path / "engine.db"
    app = AppTest.from_string(_render_script(database, mode=mode))
    app.run(timeout=30)
    button = app.button("btn_emergency_stop")
    assert button is not None, f"{mode} did not render the halt control"
    assert button.disabled is False, f"{mode} rendered a disabled halt"
    assert not _command_rows(database), "rendering must not queue anything"

    button.click().run(timeout=30)
    rows = _command_rows(database)
    assert rows and rows[0]["command"] == "EMERGENCY_STOP"
    assert json.loads(rows[0]["params_json"])["close_positions"] is True


def test_the_sweep_step_is_optional_and_defaults_on(tmp_path):
    """The operator chooses whether the halt also flattens, before clicking.

    Default on because a stop raised in a hurry is usually about exposure.
    Default off would make the dangerous case the one that needs a click.
    """
    from streamlit.testing.v1 import AppTest

    database = tmp_path / "engine.db"
    app = AppTest.from_string(_render_script(database))
    app.run(timeout=30)
    checkbox = app.checkbox[0]
    assert checkbox.value is True, "the sweep must default to on"
    assert "managed positions" in checkbox.label

    # Unchecking must reach the command: halt-only is a real operator wish.
    checkbox.uncheck().run(timeout=30)
    app.button("btn_emergency_stop").click().run(timeout=30)
    rows = _command_rows(database)
    assert rows and rows[0]["command"] == "EMERGENCY_STOP"
    assert json.loads(rows[0]["params_json"])["close_positions"] is False


def test_a_halted_engine_offers_the_typed_reset(tmp_path):
    from streamlit.testing.v1 import AppTest

    database = tmp_path / "engine.db"
    app = AppTest.from_string(_render_script(database, state="halted"))
    app.run(timeout=30)
    reset = app.button("btn_reset_emergency")
    assert reset is not None

    reset.click().run(timeout=30)
    confirm = app.button("btn_emergency_reset_confirm")
    assert confirm.disabled is True, "an empty phrase must not confirm"

    app.text_input[0].set_value("RESET").run(timeout=30)
    confirm = app.button("btn_emergency_reset_confirm")
    assert confirm.disabled is False
    confirm.click().run(timeout=30)
    rows = _command_rows(database)
    assert rows and rows[0]["command"] == "RESET_EMERGENCY"
    assert json.loads(rows[0]["params_json"])["confirmation"] == "RESET"


def test_the_header_renders_the_halt_control():
    """The control must be mounted where an operator can reach it in one glance."""
    header = Path(__file__).resolve().parents[2] / "ui" / "components" / "header.py"
    source = header.read_text(encoding="utf-8")
    assert "render_emergency_stop" in source


# ─────────────────────────────────────────────────────────────────────────────
# Schema guard
# ─────────────────────────────────────────────────────────────────────────────

def _schema2(path: Path) -> None:
    """A journal at schema 2: commands exists, scan_symbols does not."""
    connection = sqlite3.connect(path)
    try:
        for statement in list(MIGRATION_1) + list(MIGRATION_2):
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (2, utc_now_iso()),
        )
        connection.commit()
    finally:
        connection.close()


def _schema_snapshot(path: Path) -> tuple[int, set[str]]:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        version = connection.execute(
            "SELECT MAX(version) FROM schema_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        return int(version), tables
    finally:
        connection.close()


def test_schema2_halt_and_reset_queue_without_migration(tmp_path):
    """A schema-2 file already has commands (since v1). Queue both rows.

    Opening Journal would migrate the file. This path must not.
    """
    database = tmp_path / "old_engine.db"
    _schema2(database)
    before = _schema_snapshot(database)
    assert before[0] == 2
    assert "commands" in before[1]
    assert "scan_symbols" not in before[1]

    halt_id = queue_emergency_stop(database, close_positions=False)
    reset_id = queue_reset_emergency(database, confirmation=RESET_PHRASE)

    after = _schema_snapshot(database)
    assert after == before
    rows = _command_rows(database)
    assert [row["command"] for row in rows] == ["EMERGENCY_STOP", "RESET_EMERGENCY"]
    assert halt_id == 1 and reset_id == 2
    assert json.loads(rows[0]["params_json"])["close_positions"] is False
    assert json.loads(rows[1]["params_json"])["confirmation"] == "RESET"


def test_a_locked_journal_raises_after_retry(tmp_path):
    """The insert retries a lock, then reports it. It does not migrate."""
    database = tmp_path / "locked.db"
    _schema2(database)
    holder = sqlite3.connect(database)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(JournalFileUnavailable, match="locked"):
            queue_emergency_stop(database)
    finally:
        holder.close()
    assert _command_rows(database) == []
    assert _schema_snapshot(database)[0] == 2
    assert "scan_symbols" not in _schema_snapshot(database)[1]


def test_a_missing_journal_is_unreadable(tmp_path):
    missing = tmp_path / "no-such-journal.db"
    with pytest.raises(JournalFileUnavailable, match="unreadable"):
        queue_emergency_stop(missing)
    assert not missing.exists()


def test_schema2_click_queues_and_shows_no_schema_error(tmp_path):
    """The button still halts when the journal is older than the code."""
    from streamlit.testing.v1 import AppTest

    database = tmp_path / "old_engine.db"
    _schema2(database)
    before = _schema_snapshot(database)

    app = AppTest.from_string(_render_script(database, mode="PAPER", state="running"))
    app.run(timeout=30)
    button = app.button("btn_emergency_stop")
    assert button is not None
    assert button.disabled is False

    button.click().run(timeout=30)

    assert not app.exception, "a schema-2 halt must not surface a traceback"
    assert len(app.error) == 0
    rows = _command_rows(database)
    assert rows and rows[0]["command"] == "EMERGENCY_STOP"
    assert _schema_snapshot(database) == before


def test_a_locked_journal_shows_a_retry_message(tmp_path):
    """The operator sees the lock message. The click does not traceback."""
    from streamlit.testing.v1 import AppTest

    database = tmp_path / "locked_ui.db"
    _schema2(database)
    holder = sqlite3.connect(database)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        app = AppTest.from_string(_render_script(database, mode="PAPER", state="running"))
        app.run(timeout=30)
        app.button("btn_emergency_stop").click().run(timeout=30)
        assert not app.exception
        messages = [str(item.value) for item in app.error]
        assert any("locked" in message.lower() for message in messages)
        assert _command_rows(database) == []
    finally:
        holder.close()
