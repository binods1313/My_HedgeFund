"""Read-only Signals page with status tabs and decision-chain view.

This page shows signals grouped by status in tabs. It is read-only by design:
no approval buttons, no executor calls, no order submission. Signals are
written by the scan cycle and reviewed here; approval lives in Stage 2c.

Tabs: Pending/Awaiting Approval, Approved, Rejected, Expired, Superseded,
Executed, Failed.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from ui.components.cards import accent_card
from ui.data_access import read_journal_table

_TAB_STATUSES = [
    ("PENDING", "Pending / Awaiting Approval"),
    ("APPROVED", "Approved"),
    ("REJECTED", "Rejected"),
    ("EXPIRED", "Expired"),
    ("SUPERSEDED", "Superseded"),
    ("EXECUTED", "Executed"),
    ("FAILED", "Failed"),
]

_SIGNAL_COLUMNS = [
    "id", "symbol", "direction", "score", "bar_ts", "ts", "decision_id",
    "status", "strategy", "timeframe", "entry_price", "stop_loss",
    "take_profit", "volume", "expires_at", "decided_by", "decided_at",
    "reject_reason", "is_test", "test_tag",
]


def render_signals_page(db_path=None) -> None:
    """Render the read-only Signals page with status tabs."""
    labels = [label for _, label in _TAB_STATUSES]
    default = "Pending / Awaiting Approval"

    selected = st.segmented_control(
        "Signal status",
        options=labels,
        default=default,
        key="signals_tab",
    )

    if selected is None:
        selected = default
    chosen_status = next(
        (s for s, label in _TAB_STATUSES if label == selected),
        "PENDING",
    )

    _render_tab(chosen_status, db_path=db_path)
    _render_decision_chain(db_path=db_path)


def _render_tab(status: str, *, db_path) -> None:
    """Read signals for one status and render them as a read-only table."""
    try:
        frame = read_journal_table(
            "signals",
            db_path=db_path,
            limit=500,
            symbol=None,
        )
    except ValueError:
        st.warning("Signals table is not available in this journal.")
        return

    if frame is None or frame.empty:
        st.caption(f"No signals with status {status} recorded yet.")
        return

    filtered = frame[frame["status"] == status].copy()

    if filtered.empty:
        st.caption(f"No signals with status {status} recorded yet.")
        return

    display = _format_signals(filtered)

    with accent_card("violet", "signals-table"):
        st.caption(f"Status: {status} · {len(display)} row(s) · read-only")
        st.dataframe(
            display,
            width="stretch",
            hide_index=True,
            use_container_width=True,
        )

    _render_test_signal_warning(filtered)


def _format_signals(frame: pd.DataFrame) -> pd.DataFrame:
    """Select and rename columns for display."""
    cols = [c for c in _SIGNAL_COLUMNS if c in frame.columns]
    shown = frame[cols].copy()
    rename = {}
    if "decision_id" in shown.columns:
        rename["decision_id"] = "Decision ID"
    if "entry_price" in shown.columns:
        rename["entry_price"] = "Entry"
    if "stop_loss" in shown.columns:
        rename["stop_loss"] = "Stop"
    if "take_profit" in shown.columns:
        rename["take_profit"] = "TP"
    if "expires_at" in shown.columns:
        rename["expires_at"] = "Expires"
    if "decided_by" in shown.columns:
        rename["decided_by"] = "By"
    shown = shown.rename(columns=rename)
    # Truncate decision IDs for display.
    if "decision_id" in shown.columns or "Decision ID" in shown.columns:
        col = "Decision ID" if "Decision ID" in shown.columns else "decision_id"
        shown[col] = shown[col].astype(str).str[:12] + "…"
    return shown


def _render_test_signal_warning(frame: pd.DataFrame) -> None:
    """Show a quiet notice when test signals are visible."""
    if "is_test" not in frame.columns:
        return
    test_count = int((frame["is_test"] == 1).sum())
    if test_count > 0:
        st.caption(f"{test_count} TEST SIGNAL row(s) shown. Excluded from production P&L.")


def _render_decision_chain(*, db_path) -> None:
    """When a decision ID is selected, show the lifecycle chain for it."""
    decision_id = st.session_state.get("_signals_decision_id")
    if not decision_id:
        st.caption(
            "Enter a decision ID below to trace a signal's full lifecycle chain."
        )
        decision_id = st.text_input(
            "Decision ID", key="_signals_decision_id",
            placeholder="e.g. a3f2c1e4…",
        )
        if not decision_id:
            return

    try:
        frame = read_journal_table(
            "lifecycle",
            db_path=db_path,
            limit=100,
            decision_id=decision_id,
        )
    except ValueError:
        st.caption("Lifecycle view is not available for this journal.")
        return

    if frame is None or frame.empty:
        st.caption(f"No lifecycle events found for decision ID {decision_id}.")
        return

    with accent_card("cyan", "decision-chain"):
        st.caption(f"Decision chain for {decision_id[:12]}…")
        st.dataframe(
            frame,
            width="stretch",
            hide_index=True,
            use_container_width=True,
        )
        st.caption(f"{len(frame)} event(s) · read-only")
