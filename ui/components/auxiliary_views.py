from __future__ import annotations

import streamlit as st

from formatting import format_money
from ui.components.cards import accent_card, render_html_table, render_journal_table
from ui.components.snapshot import RunSnapshot


def render_portfolio_view(snapshot: RunSnapshot) -> None:
    account = snapshot.account
    st.subheader("Portfolio")
    currency = account.currency if account and account.currency else "—"
    columns = st.columns(4, gap="small")
    metrics = (
        ("Balance", format_money(account.balance, currency) if account else "—", "green"),
        ("Equity", format_money(account.equity, currency) if account else "—", "green"),
        ("Free margin", format_money(account.free_margin, currency) if account else "—", "cyan"),
        ("Open P&L", format_money(account.open_pnl, currency) if account else "—", "green"),
    )
    for column, (label, value, accent) in zip(columns, metrics):
        with column:
            with accent_card(accent, f"portfolio-{label.lower().replace(' ', '-')}"):
                st.metric(label, value)
    with accent_card("violet", "portfolio-exposure"):
        st.caption("LIVE OPEN EXPOSURE")
        render_html_table(snapshot.positions, empty="No active open positions")


def render_journal_view(snapshot: RunSnapshot) -> None:
    st.subheader("Engine journal")
    with accent_card("cyan", "journal"):
        st.caption("READ-ONLY ENGINE EVENTS")
        st.caption("Relative times such as 2h ago. Hover a time for the absolute IST timestamp.")
        render_journal_table(snapshot.events)
