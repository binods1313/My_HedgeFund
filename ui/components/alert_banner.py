"""One dismissible banner. It never submits, closes, or blocks the page."""

from __future__ import annotations

import streamlit as st

from ui.alerts import (
    MARGIN_WARN_LEVEL,
    alert_signature,
    banner_lines,
    margin_is_low,
    risk_exceeds_cap,
    should_show,
)
from ui.components.snapshot import tickets_without_stop


def render_alert_banner(snapshot, cap_pct: float) -> None:
    """Show SL=0, low margin, or open-risk once per signature."""
    if snapshot is None:
        return
    account = snapshot.account
    tickets = tickets_without_stop(getattr(snapshot, "positions", None))
    margin_low = margin_is_low(getattr(account, "margin_level", None), MARGIN_WARN_LEVEL)
    risk_high = risk_exceeds_cap(
        getattr(account, "open_risk", None),
        getattr(account, "equity", None),
        cap_pct,
    )
    signature = alert_signature(tickets, margin_low=margin_low, risk_high=risk_high)
    if not should_show(signature, st.session_state.get("_alert_dismissed")):
        return
    lines = banner_lines(
        tickets,
        margin_low=margin_low,
        margin_level=getattr(account, "margin_level", None),
        warn_level=MARGIN_WARN_LEVEL,
        risk_high=risk_high,
        cap_pct=cap_pct,
    )
    st.warning(" · ".join(lines))
    if st.button("Dismiss", key="btn_dismiss_alert"):
        st.session_state["_alert_dismissed"] = signature
