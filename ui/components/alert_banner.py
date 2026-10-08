"""One dismissible banner. It never submits, closes, or blocks the page."""

from __future__ import annotations

import streamlit as st

from ui.alerts import (
    MARGIN_WARN_LEVEL,
    alert_signature,
    banner_lines,
    margin_is_low,
    margin_notice_text,
    risk_exceeds_cap,
    should_show,
)
from ui.components.snapshot import tickets_without_stop


def render_alert_banner(snapshot, cap_pct: float) -> None:
    """Show SL=0, low margin, or open-risk once per signature."""
    if snapshot is None:
        return
    account = snapshot.account
    render_margin_notice(getattr(account, "margin_level", None), MARGIN_WARN_LEVEL)
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


def render_margin_notice(margin_level: float | None, warn_level: float = MARGIN_WARN_LEVEL) -> None:
    """Sound-free notice. Dismiss hides this warn level only. It does not send an order."""
    text = margin_notice_text(margin_level, warn_level)
    if text is None:
        return
    signature = f"margin:{warn_level:.1f}"
    if not should_show(signature, st.session_state.get("_margin_notice_dismissed")):
        return
    st.warning(text)
    if st.button("Dismiss margin notice", key="btn_dismiss_margin_notice"):
        st.session_state["_margin_notice_dismissed"] = signature
