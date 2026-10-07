"""Research quote button. Not inside the 2s header fragment and not an order price."""

from __future__ import annotations

import streamlit as st

from ui.services.infoway_client import fetch_quote


def render_infoway_research(symbol: str | None) -> None:
    st.caption("Infoway is research only. It is not the order price. MT5 is the source of truth.")
    if st.button("Show Infoway quote", key="btn_infoway_quote"):
        st.session_state["infoway_visible"] = True
    if not st.session_state.get("infoway_visible"):
        return
    if not symbol:
        st.caption("Infoway · no symbol")
        return
    result = fetch_quote(symbol)
    if result.status == "ok" and result.price:
        st.caption(f"Infoway {result.symbol} · {result.price}")
    else:
        st.caption(f"Infoway · {result.status}")
