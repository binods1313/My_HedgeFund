from __future__ import annotations

import time

import streamlit as st

from formatting import format_money
from ui.components.cards import accent_card, render_html_table, render_journal_table
from ui.components.data_cache import equity_history
from ui.components.journal_tables import render_journal_tables, render_system_status
from ui.components.snapshot import RunSnapshot
from ui.equity_curve import clean_equity_points
from ui.data_access import get_production_stats
from ui.performance import performance_report, slice_window


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
    render_performance(currency, _portfolio_equity(snapshot))


def render_performance(currency: str, values: list[float]) -> None:
    """Snapshot change/drawdown/Sharpe; win/loss from production trades only."""
    with accent_card("green", "portfolio-performance"):
        st.subheader("Performance")
        st.caption("Equity change, drawdown, Sharpe: broker equity snapshots")
        stats = get_production_stats()
        realised = stats["realised"] or None
        window = st.segmented_control(
            "Equity window",
            ["All", "Last 30", "Last 7"],
            default="All",
            key="w_perf_window",
            width="stretch",
            wrap=True,
        )
        report = performance_report(
            slice_window(values, window or "All"), realised=realised)
        st.caption(str(report["annualisation"]))
        change = report["change"]
        drawdown = report["max_drawdown_pct"]
        sharpe = report["sharpe"]
        columns = st.columns(3, gap="small")
        with columns[0]:
            if change is None:
                st.metric("Equity change", "—")
            else:
                st.metric("Equity change", format_money(change[0], currency), delta=f"{change[1]:+.2f}%")
        with columns[1]:
            st.metric("Max drawdown", "—" if drawdown is None else f"{drawdown:.2f}%")
        with columns[2]:
            st.metric("Sharpe", "—" if sharpe is None else f"{sharpe:.2f}")
        if "win_loss" in report:
            ratio = report["win_loss"]
            st.metric("Win/loss", "—" if ratio is None else f"{ratio:.2f}")
        st.caption(
            "Win/loss: closed production trades (TEST SIGNAL excluded). "
            + str(stats["sources"]["win_loss"])
        )


def _portfolio_equity(snapshot: RunSnapshot) -> list[float]:
    history = equity_history(int(time.time()))
    columns = getattr(history, "columns", [])
    stamps = list(history["ts"]) if history is not None and "ts" in columns else []
    amounts = list(history["equity"]) if history is not None and "equity" in columns else []
    return [point[1] for point in clean_equity_points(stamps, amounts)]


def render_journal_view(snapshot: RunSnapshot) -> None:
    st.subheader("Engine journal")
    render_system_status()
    with accent_card("cyan", "journal"):
        st.caption("READ-ONLY ENGINE EVENTS")
        st.caption("Relative times such as 2h ago. Hover a time for the absolute IST timestamp.")
        render_journal_table(snapshot.events)
    render_journal_tables()
