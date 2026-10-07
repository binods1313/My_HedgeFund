"""Research quote button. Not inside the 2s header fragment and not an order price."""

from __future__ import annotations

import time

import streamlit as st

from ui.services.infoway_client import budget_snapshot, fetch_quote


def render_infoway_chip() -> None:
    """Status only. This does not call Infoway."""
    count, cap = budget_snapshot()
    calls = "unreadable" if count is None else f"{count} / {cap}"
    fetched = st.session_state.get("infoway_fetched_at")
    if isinstance(fetched, (int, float)):
        age = max(0, int(time.time() - float(fetched)))
        age_text = f"{age}s"
    else:
        age_text = "no read yet"
    with st.container(border=True):
        st.text("Secondary data (research only)")
        st.caption(f"source: Infoway · data age {age_text} · calls today {calls}")
        st.caption("MT5 is the only price source for orders.")


def render_infoway_research(symbol: str | None) -> None:
    render_infoway_chip()
    if st.button("Show Infoway quote", key="btn_infoway_quote"):
        st.session_state["infoway_visible"] = True
    if not st.session_state.get("infoway_visible"):
        return
    if not symbol:
        st.caption("Infoway · no symbol")
        return
    result = fetch_quote(symbol)
    st.session_state["infoway_fetched_at"] = time.time()
    if result.status == "ok" and result.price:
        st.caption(f"Infoway {result.symbol} · {result.price}")
    else:
        st.caption(f"Infoway · {result.status}")
