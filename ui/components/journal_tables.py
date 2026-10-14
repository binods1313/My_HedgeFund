"""Read-only Journal tables and the System Status card.

Every frame here comes from `ui.data_access.read_journal_table`, which opens
the journal in SQLite's read-only mode and never creates a file. There are no
action buttons in this module by design: a history view that can act on its
rows is a trading surface wearing a history view's clothes.
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from ui.components.cards import accent_card
from ui.data_access import JOURNAL_TABLES, read_journal_table, system_status

#: Human labels for the table whitelist, in the order the Journal tab shows
#: them. The keys are the `read_journal_table` names; nothing else is passed
#: to the reader, so this widget cannot name a table outside the whitelist.
_TABLE_LABELS: dict[str, str] = {
    "signals": "Signals",
    "risk_decisions": "Risk decisions",
    "llm_calls": "AI calls",
    "scan_results": "Scan results",
    "trades": "Trades",
    "positions": "Positions",
    "lifecycle": "Trade lifecycle",
}

#: Column headers rendered plainly instead of their JSON-ish names.
_RENAME = {
    "reasons_json": "reasons",
    "symbols_json": "symbols",
    "ranked_json": "ranked",
    "decision_id": "decision ID",
}

#: Lifecycle rows carry a JSON context blob the table does not surface;
#: the decision ID is already lifted into its own column by the reader.
_JSON_COLUMNS = ("reasons_json", "symbols_json", "ranked_json")


def render_journal_tables(db_path=None) -> None:
    """Table picker, filters, and a read-only frame of the chosen table."""
    labels = [_TABLE_LABELS[name] for name in JOURNAL_TABLES]
    label_to_name = {_TABLE_LABELS[name]: name for name in JOURNAL_TABLES}
    with accent_card("cyan", "journal-tables"):
        st.caption("READ-ONLY JOURNAL TABLES")
        chosen_label = st.selectbox("Table", labels, key="journal_table")
        name = label_to_name.get(chosen_label, "signals")

        filter_cols = st.columns([2, 2, 1], gap="small")
        with filter_cols[0]:
            symbol = st.text_input(
                "Symbol filter", key="journal_symbol",
                placeholder="e.g. EURUSD",
                help="Applies to tables that carry a symbol; left empty "
                     "shows everything.",
            ) or None
        with filter_cols[1]:
            decision_id = None
            if name == "lifecycle":
                decision_id = st.text_input(
                    "Decision ID", key="journal_decision_id",
                    placeholder="e.g. 20260114-1201-A7F3",
                    help="One trade's chain: signal to risk to order to close.",
                ) or None
        with filter_cols[2]:
            limit = st.number_input(
                "Rows", min_value=10, max_value=1000, value=200, step=10,
                key="journal_limit",
            )

        try:
            frame = read_journal_table(
                name, db_path=db_path, limit=int(limit),
                symbol=symbol, decision_id=decision_id,
            )
        except ValueError as exc:
            # A filter that does not fit the chosen table: say so rather
            # than rendering an unfiltered list under a filtered heading.
            st.warning(str(exc))
            return
        _draw_frame(name, frame)


def _draw_frame(name: str, frame: pd.DataFrame) -> None:
    if frame.empty:
        st.caption(
            f"No {name.replace('_', ' ')} recorded yet. "
            "An empty table is a fact about the journal, not an error."
        )
        return
    shown = frame.copy()
    if name == "trades":
        shown["side"] = shown["side"].astype(str).str.upper()
    for column in _JSON_COLUMNS:
        if column in shown.columns:
            shown[column] = shown[column].map(_pretty_json)
    shown = shown.rename(columns=_RENAME)
    st.dataframe(shown, width="stretch", hide_index=True)
    st.caption(f"{len(shown)} row(s), newest first · read-only")


def _pretty_json(value: object) -> str:
    try:
        parsed = json.loads(str(value)) if value else None
    except (TypeError, ValueError):
        return str(value or "")
    if isinstance(parsed, list):
        return ", ".join(str(item) for item in parsed) or "—"
    if isinstance(parsed, dict):
        return ", ".join(f"{key}: {val}" for key, val in parsed.items()) or "—"
    return str(parsed) if parsed is not None else "—"


def render_system_status(db_path=None) -> None:
    """One card of journal-derived facts. Nothing here talks to a process."""
    status = system_status(db_path)
    with accent_card("cyan", "system-status"):
        st.caption("SYSTEM STATUS · READ-ONLY JOURNAL VIEW")
        if not status["journal_exists"]:
            st.caption(
                f"No journal at {status['journal_path']}. "
                "The engine writes one on first run."
            )
            return
        online = "ONLINE" if status["engine_online"] else "OFFLINE"
        state = status["engine_state"] or "—"
        mode = status["mode"] or "—"
        age = status["heartbeat_age_s"]
        age_text = "—" if age is None else f"{age:.0f}s ago"
        account = status["account_login_masked"] or "—"
        currency = status["account_currency"] or ""
        columns = st.columns(4, gap="small")
        pending = status["pending_commands"]
        facts = (
            ("Engine", f"{online} · {state} ({mode})"),
            ("Heartbeat", age_text),
            ("Account", f"{account} {currency}".strip()),
            ("Journal", f"v{status['schema_version'] or '—'} · "
                        f"{pending if pending is not None else '—'} "
                        "pending command(s)"),
        )
        for column, (label, value) in zip(columns, facts):
            with column:
                st.metric(label, value)
        st.caption(f"journal: {status['journal_path']}")

