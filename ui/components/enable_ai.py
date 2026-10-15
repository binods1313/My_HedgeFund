"""Paper-only ENABLE_AI control. It writes the command queue and never sends an order."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

def enable_ai_availability(mode: str | None) -> tuple[bool, str]:
    """Paper may queue the command. LIVE and anything else stay disabled."""
    normalized = (mode or "").strip().upper()
    if normalized == "LIVE":
        return False, "Disabled in LIVE mode. Paper mode only."
    if normalized != "PAPER":
        return False, "Arming is available in PAPER mode only."
    return True, ""


def queue_enable_ai(db_path: Path | None = None) -> int:
    """Insert ENABLE_AI. The engine polls the row. This function does not trade."""
    from engine.commands import COMMAND_ENABLE_AI
    from engine.journal import Journal

    # The caller may pass an explicit journal; otherwise the one configured path
    # is used. There is no default, because a default is how a UI button ends up
    # writing into a journal the engine is not reading.
    target = db_path
    if target is None:
        from engine.config import load_engine_config
        from engine.journal import resolve_db_path
        target = resolve_db_path(load_engine_config().engine.db_path)

    journal = Journal(target)
    try:
        return journal.enqueue_command(COMMAND_ENABLE_AI, {"source": "ui"})
    finally:
        journal.close()


def render_enable_ai(mode: str | None, *, db_path: Path | None = None) -> None:
    enabled, reason = enable_ai_availability(mode)
    if not enabled:
        st.button("Arm paper engine", key="btn_enable_ai",
                  disabled=True, help=reason)
        st.caption(reason)
        return
    if st.button(
        "Arm paper engine",
        key="btn_enable_ai",
        help="Queue ENABLE_AI: OFF to IDLE. Arms the loop; "
        "starts no scan, no strategy, no orders.",
    ):
        st.session_state["_enable_ai_ask"] = True
    if st.session_state.get("_enable_ai_ask"):
        _confirm_enable_ai(db_path)


@st.dialog("Arm paper engine", width="small")
def _confirm_enable_ai(db_path: Path | None) -> None:
    st.write("Queue ENABLE_AI on the paper engine. "
             "Moves state OFF to IDLE only. "
             "Starts no scan, no strategy, no risk run, no orders.")
    cancel_col, confirm_col = st.columns(2)
    with cancel_col:
        if st.button("Cancel", key="btn_enable_ai_cancel", width="stretch"):
            st.session_state["_enable_ai_ask"] = False
            st.rerun()
    with confirm_col:
        if st.button("Confirm arm engine", key="btn_enable_ai_confirm",
                     type="primary", width="stretch"):
            queue_enable_ai(db_path)
            st.session_state["_enable_ai_ask"] = False
            st.rerun()
