"""Emergency stop and reset controls. They write the command queue and never send an order.

The engine half already exists and is proven (`engine/commands.py::
_cmd_emergency_stop`, `engine/state.py` terminal EMERGENCY_STOP). This module is
the dashboard half: one click enqueues `EMERGENCY_STOP`, which the running
engine claims on its next poll and executes without asking anything -- an
emergency must not wait on a dialog. Leaving the state is the deliberate
direction: `RESET_EMERGENCY` requires the operator to type the exact phrase
`RESET`, which the engine's state machine validates, not this module.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

#: The exact phrase the engine's state machine requires to leave EMERGENCY_STOP.
#: Defined here so the UI cannot drift from `engine/state.py`; the engine is
#: still the authority -- a wrong phrase queues and then fails loudly.
RESET_PHRASE = "RESET"


def emergency_stop_availability(mode: str | None, engine_state: str | None) -> tuple[bool, str]:
    """A halt may be queued in every mode. No mode ever disables it.

    PAPER, DEMO and LIVE all offer the same control, deliberately. The halt is
    the operator's last resort, so its availability must not depend on the one
    variable they may be least able to trust: the mode string. All three modes
    enqueue the same command into the same queue, and the same state machine
    validates it -- so a halt is safe to offer everywhere. Refusing it because
    the mode read was unrecognised or because the account happens to be live
    would leave an operator watching a runaway loop with no way to stop it.

    The previous version disabled LIVE on the reasoning that a paper-only
    system has no automated loop for a halt to stop. That reasoning stops
    holding the moment a scan loop exists -- which is exactly Phase 2A -- so
    the restriction had to go before the loop arrived, not after.

    The one state that disables it is already being halted, because the reset
    control takes that place and a second stop would imply one is still
    needed. ``mode`` is still accepted so the call site and the audit trail
    keep naming the mode the halt was raised in; it no longer gates anything.
    """
    if (engine_state or "").strip().lower() == "halted":
        return False, "Emergency stop is already active."
    return True, ""


def queue_emergency_stop(db_path: Path | None = None, *, close_positions: bool = True) -> int:
    """Insert EMERGENCY_STOP. The engine polls the row; this never trades."""
    from engine.commands import COMMAND_EMERGENCY_STOP
    from engine.journal import Journal, SchemaTooOld

    # Same resolution rule as ENABLE_AI: an explicit journal in tests, the one
    # configured path in production, and no implicit default in between.
    target = db_path
    if target is None:
        from engine.config import load_engine_config
        from engine.journal import resolve_db_path
        target = resolve_db_path(load_engine_config().engine.db_path)

    try:
        journal = Journal(target, migrate=False)
    except SchemaTooOld:
        raise SchemaTooOld(SchemaTooOld.TEXT) from None
    try:
        return journal.enqueue_command(
            COMMAND_EMERGENCY_STOP,
            {"source": "ui", "close_positions": bool(close_positions)},
        )
    finally:
        journal.close()


def queue_reset_emergency(db_path: Path | None = None, *, confirmation: str) -> int:
    """Insert RESET_EMERGENCY with the operator's typed phrase verbatim.

    The phrase is validated by the engine's state machine; passing it through
    rather than re-checking it here keeps a single authority for the rule.
    """
    from engine.commands import COMMAND_RESET_EMERGENCY
    from engine.journal import Journal, SchemaTooOld

    target = db_path
    if target is None:
        from engine.config import load_engine_config
        from engine.journal import resolve_db_path
        target = resolve_db_path(load_engine_config().engine.db_path)

    try:
        journal = Journal(target, migrate=False)
    except SchemaTooOld:
        raise SchemaTooOld(SchemaTooOld.TEXT) from None
    try:
        return journal.enqueue_command(
            COMMAND_RESET_EMERGENCY,
            {"source": "ui", "confirmation": str(confirmation)},
        )
    finally:
        journal.close()


def render_emergency_stop(mode: str | None, engine_state: str | None, *,
                          db_path: Path | None = None) -> None:
    """Halt button while the engine runs; typed-reset flow while it is halted."""
    if (engine_state or "").strip().lower() == "halted":
        st.caption("Emergency stop is active. Clearing it requires typing the phrase.")
        if st.button("Clear emergency stop", key="btn_reset_emergency",
                     help=f"Type {RESET_PHRASE} to confirm. The engine stays halted until then."):
            st.session_state["_emergency_reset_ask"] = True
        if st.session_state.get("_emergency_reset_ask"):
            _confirm_reset_emergency(db_path)
        return

    enabled, reason = emergency_stop_availability(mode, engine_state)
    if not enabled:
        st.button("Emergency stop", key="btn_emergency_stop", disabled=True, help=reason)
        st.caption(reason)
        return

    # The optional second half of the halt, chosen before it is raised rather
    # than asked about afterwards: an emergency must not wait on a dialog, but
    # "halt" and "halt and flatten" are different wishes and only the operator
    # knows which one they mean. Default on, because a stop raised in a hurry
    # is usually about exposure. Sweeping is managed-magic only -- the engine's
    # `_close_everything` closes journal positions, and the guard never writes
    # a foreign-magic position into the journal, so foreign positions are
    # structurally out of reach here. The caption says so, because an operator
    # deserves to know what will *not* happen before they click.
    close_positions = st.checkbox(
        "Also close all my managed positions",
        key="emergency_close_positions",
        value=True,
        help="Closes only positions this system opened. Positions carrying "
             "another magic number are never touched.",
    )
    if st.button(
        "Emergency stop",
        key="btn_emergency_stop",
        help="Queue EMERGENCY_STOP: halts the engine immediately. "
             "No confirmation dialog; the engine acts on the next poll.",
    ):
        queue_emergency_stop(db_path, close_positions=bool(close_positions))
        st.rerun()


@st.dialog("Clear emergency stop", width="small")
def _confirm_reset_emergency(db_path: Path | None) -> None:
    st.write(
        f"Emergency stop is terminal. Type {RESET_PHRASE} to clear it. "
        "The engine returns to OFF; arming is a separate step."
    )
    typed = st.text_input(f"Type {RESET_PHRASE} to confirm", key="emergency_reset_phrase")
    cancel_col, confirm_col = st.columns(2)
    with cancel_col:
        if st.button("Cancel", key="btn_emergency_reset_cancel", width="stretch"):
            st.session_state["_emergency_reset_ask"] = False
            st.rerun()
    with confirm_col:
        if st.button(
            "Confirm reset", key="btn_emergency_reset_confirm", type="primary",
            width="stretch", disabled=str(typed or "").strip() != RESET_PHRASE,
        ):
            queue_reset_emergency(db_path, confirmation=RESET_PHRASE)
            st.session_state["_emergency_reset_ask"] = False
            st.rerun()