"""Scrollable pattern list for the market workspace.

Hits are display data. This panel does not call the engine, does not score
a pattern with an agent, and does not submit an order.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ui.patterns import (
    describe_trigger,
    marker_label,
    pattern_group,
    pattern_key,
    sentiment_text,
)


def render_pattern_matrix(
    asset: str | None,
    frame: pd.DataFrame,
    hits: list,
    notes: dict[int, list[str]],
    on_deploy,
) -> None:
    st.markdown("**Pattern matrix**")
    st.caption(
        "Display only. This chart carries no automated evaluation, so there is "
        "no confidence score. "
        "Deploy order copies side and stop into the order form and does not submit."
    )
    hidden = st.session_state.get("pattern_hidden") or []
    if hidden:
        st.caption(f"{len(hidden)} hidden in this session.")
        if st.button("Restore hidden patterns", key="btn_pattern_restore", width="content"):
            st.session_state["pattern_hidden"] = []
    if not hits:
        st.caption("No pattern in this window for the current filter.")
        return
    newest = list(reversed(hits))[:40]
    if len(hits) > 40:
        st.caption(f"Showing the latest 40 of {len(hits)}.")
    opens = [float(value) for value in frame["open"]]
    highs = [float(value) for value in frame["high"]]
    lows = [float(value) for value in frame["low"]]
    closes = [float(value) for value in frame["close"]]
    watches = set(st.session_state.get("pattern_watches") or [])
    with st.container(height=640):
        for hit in newest:
            when = str(frame["time"].iloc[hit.index])
            row_id = pattern_key(when, hit.name)
            with st.container(border=True):
                st.markdown(f"**{marker_label(hit.name)} · {hit.name}**")
                st.caption(f"{when} · {pattern_group(hit.name)} · {hit.bias}")
                st.caption(sentiment_text(hit.name))
                st.caption(describe_trigger(opens, highs, lows, closes, hit.index, hit.name))
                st.caption("Not evaluated")
                chart_notes = notes.get(hit.index) or []
                if chart_notes:
                    st.caption("Chart math: " + " · ".join(chart_notes))
                if row_id in watches:
                    st.caption("Session watch. The engine was not notified.")
                can_copy = hit.bias != "neutral" and hit.sl is not None and asset is not None
                if st.button(
                    "Deploy order",
                    key=f"btn_pat_deploy_{row_id}",
                    width="stretch",
                    disabled=not can_copy,
                    help="Copies side and stop into the order form. Does not submit.",
                ):
                    on_deploy(hit)
                if st.button(
                    "Invalidate signal",
                    key=f"btn_pat_hide_{row_id}",
                    width="stretch",
                    help="Hides this mark in this session. The engine is not changed.",
                ):
                    _hide(row_id)
                if st.button(
                    "Set alert",
                    key=f"btn_pat_watch_{row_id}",
                    width="stretch",
                    help="Saves a session watch. The engine is not notified.",
                ):
                    _watch(row_id)


def _hide(row_id: str) -> None:
    hidden = set(st.session_state.get("pattern_hidden") or [])
    hidden.add(row_id)
    st.session_state["pattern_hidden"] = sorted(hidden)


def _watch(row_id: str) -> None:
    watches = list(st.session_state.get("pattern_watches") or [])
    if row_id not in watches:
        watches.append(row_id)
    st.session_state["pattern_watches"] = watches
