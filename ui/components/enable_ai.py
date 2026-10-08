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
        return False, "Enable AI is available in PAPER mode only."
    return True, ""


def queue_enable_ai(db_path: Path | None = None) -> int:
    """Insert ENABLE_AI. The engine polls the row. This function does not trade."""
    from engine.commands import COMMAND_ENABLE_AI
    from engine.journal import Journal, resolve_db_path

    journal = Journal(db_path or resolve_db_path())
    try:
        return journal.enqueue_command(COMMAND_ENABLE_AI, {"source": "ui"})
    finally:
        journal.close()


def render_enable_ai(mode: str | None, *, db_path: Path | None = None) -> None:
    enabled, reason = enable_ai_availability(mode)
    if not enabled:
        st.button("Enable AI", key="btn_enable_ai", disabled=True, help=reason)
        st.caption(reason)
        return
    if st.button(
        "Enable AI",
        key="btn_enable_ai",
        help="Queue ENABLE_AI. This does not send an order.",
    ):
        st.session_state["_enable_ai_ask"] = True
    if st.session_state.get("_enable_ai_ask"):
        _confirm_enable_ai(db_path)


@st.dialog("Enable AI", width="small")
def _confirm_enable_ai(db_path: Path | None) -> None:
    st.write("Queue ENABLE_AI on the paper engine. This does not send an order.")
    cancel_col, confirm_col = st.columns(2)
    with cancel_col:
        if st.button("Cancel", key="btn_enable_ai_cancel", width="stretch"):
            st.session_state["_enable_ai_ask"] = False
            st.rerun()
    with confirm_col:
        if st.button("Confirm Enable AI", key="btn_enable_ai_confirm", type="primary", width="stretch"):
            queue_enable_ai(db_path)
            st.session_state["_enable_ai_ask"] = False
            st.rerun()
