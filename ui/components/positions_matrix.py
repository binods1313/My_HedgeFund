from __future__ import annotations

import pandas as pd
import streamlit as st

from formatting import format_money
from ui.components.cards import accent_card, render_html_table
from ui.components.snapshot import RunSnapshot
from ui.data_access import close_position


@st.dialog("Confirm position close", width="small")
def _confirm_close_dialog() -> None:
    ticket = st.session_state.get("pending_close_ticket")
    if ticket is None:
        return
    st.write(f"Close open position #{ticket}?")
    cancel_col, confirm_col = st.columns(2)
    with cancel_col:
        if st.button("Cancel", key="btn_cancel_close", width="stretch"):
            st.session_state["pending_close_ticket"] = None
            st.rerun()
    with confirm_col:
        if st.button("Confirm close", key="btn_confirm_close", type="primary", width="stretch"):
            result = close_position(int(ticket))
            st.session_state["pending_close_ticket"] = None
            st.session_state["_last_close_result"] = {
                "ok": result.ok,
                "code": result.code,
                "message": result.message,
            }
            st.rerun()


@st.dialog("Confirm emergency close", width="small")
def _confirm_close_all_dialog() -> None:
    positions = st.session_state.get("_active_positions")
    if positions is None or getattr(positions, "empty", True):
        tickets: list[int] = []
    else:
        tickets = [int(ticket) for ticket in positions.get("ticket", []) if pd.notna(ticket)]
    st.warning(f"This will send close requests for {len(tickets)} open position(s).")
    cancel_col, confirm_col = st.columns(2)
    with cancel_col:
        if st.button("Cancel", key="btn_cancel_close_all", width="stretch"):
            st.session_state["_confirm_close_all"] = False
            st.rerun()
    with confirm_col:
        if st.button("Confirm close all", key="btn_confirm_close_all", type="primary", width="stretch"):
            outcomes = [close_position(ticket) for ticket in tickets]
            closed = sum(result.ok for result in outcomes)
            st.session_state["_confirm_close_all"] = False
            st.session_state["_last_close_result"] = {
                "ok": closed == len(outcomes),
                "code": "CLOSE_ALL",
                "message": f"Close requests accepted for {closed} of {len(outcomes)} positions",
            }
            st.rerun()


def render_positions_matrix(snapshot: RunSnapshot) -> None:
    positions = snapshot.positions
    with accent_card("violet", "positions"):
        st.subheader("Positions")
        if positions is None or positions.empty:
            st.caption("No active open positions")
        else:
            visible = [name for name in ("ticket", "symbol", "side", "lots", "pnl") if name in positions.columns]
            render_html_table(positions[visible], empty="No active open positions")
            for row in positions.to_dict("records"):
                ticket = row.get("ticket")
                if ticket is None or pd.isna(ticket):
                    continue
                identity, action = st.columns([5, 1], gap="small", vertical_alignment="center")
                with identity:
                    pnl = row.get("pnl")
                    pnl_text = format_money(pnl, snapshot.account.currency if snapshot.account else None)
                    side = row.get("side")
                    arrow = "▲" if isinstance(pnl, (int, float)) and pnl == pnl and pnl > 0 else "▼" if isinstance(pnl, (int, float)) and pnl == pnl and pnl < 0 else "·"
                    st.caption(
                        f"#{ticket}  ·  {row.get('symbol')} {side}  ·  "
                        f"entry {row.get('entry')}  current {row.get('current')}  "
                        f"sl {row.get('sl')}  tp {row.get('tp')}  ·  P&L {arrow} {pnl_text}"
                    )
                with action:
                    if st.button("Close", key=f"close_{int(ticket)}", width="stretch"):
                        st.session_state["pending_close_ticket"] = int(ticket)
                        st.rerun(scope="app")

            if st.button("Emergency close all", key="btn_close_all", help="Requires confirmation", width="stretch"):
                st.session_state["_confirm_close_all"] = True
                st.rerun(scope="app")

        result = st.session_state.get("_last_close_result")
        if result:
            (st.success if result["ok"] else st.warning)(result["message"])

    pending = st.session_state.get("pending_close_ticket")
    open_tickets = set()
    if positions is not None and not positions.empty and "ticket" in positions.columns:
        open_tickets = {int(ticket) for ticket in positions["ticket"].dropna()}
    if pending is not None:
        if int(pending) not in open_tickets:
            st.session_state["pending_close_ticket"] = None
            st.warning(f"Position #{pending} is no longer open.")
        else:
            _confirm_close_dialog()
    if st.session_state.get("_confirm_close_all"):
        _confirm_close_all_dialog()
