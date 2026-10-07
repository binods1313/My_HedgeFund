from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from formatting import format_margin_level, format_money
from ui.components.auxiliary_views import render_portfolio_view
from ui.components.cards import accent_card
from ui.components.nav import TAB_LABELS, render_navigation
from ui.components.snapshot import load_shared_snapshot, tickets_without_stop
from ui.components.theme_studio import render_theme_studio
from ui.services.news import countdown_text, next_upcoming_high_impact
from ui.state import persist_asset_category, persist_selected_asset

CATEGORY_LABELS = {
    "forex": "Forex",
    "metals_commodities": "Metals & Commodities",
    "crypto": "Crypto",
    "indices": "Indices",
}

_METRIC_ACCENTS = {
    "Balance": "green",
    "Floating equity": "green",
    "Open P&L": "green",
    "Margin used": "cyan",
    "Free margin": "cyan",
    "Margin level": "cyan",
    "Open risk": "red",
    "Open positions": "violet",
}


def render_header(whitelist: dict[str, list[str]]) -> str | None:
    categories = [category for category, symbols in whitelist.items() if symbols]
    if categories:
        category_cols = st.columns([1.3, 1.6], gap="small")
        with category_cols[0]:
            st.selectbox(
                "Market sector",
                categories,
                key="w_asset_category",
                format_func=lambda item: CATEGORY_LABELS.get(item, item.title()),
                on_change=persist_asset_category,
            )
        category = st.session_state.get("asset_category")
        assets = whitelist.get(category, [])
        with category_cols[1]:
            st.selectbox(
                "Trading instrument",
                assets,
                key="w_selected_asset",
                on_change=persist_selected_asset,
                disabled=not assets,
            )
        asset = st.session_state.get("selected_asset")
    else:
        st.warning("No configured trading instruments are available.")
        asset = None

    _render_account_metrics()
    return asset


@st.fragment(run_every="2s")
def _render_account_metrics() -> None:
    phase = st.session_state.get("_snapshot_phase")
    snapshot = load_shared_snapshot(refresh=phase != "full")
    if phase == "full":
        st.session_state["_snapshot_phase"] = "fragment-free"
    _draw_status(snapshot)
    _draw_metrics(snapshot)
    if st.session_state.get("active_tab") == TAB_LABELS[1]:
        render_portfolio_view(snapshot)


def _draw_status(snapshot) -> None:
    account = snapshot.account
    status_cols = st.columns([1.2, 1.2, 1.2, 3.0], gap="small")
    with status_cols[0]:
        connection = "CONNECTED" if account.connected else "DISCONNECTED"
        connection_class = "live" if account.connected else "offline"
        st.markdown(
            f'<span class="status-badge {connection_class}">{connection} · {account.mode or "—"}</span>',
            unsafe_allow_html=True,
        )
    with status_cols[1]:
        state = (account.engine_state or "offline").upper()
        age = account.heartbeat_age_s
        if age is not None and age > 15:
            state = "STALE"
        if state in {"HALTED", "PAUSED", "EMERGENCY_STOP"}:
            engine_class = "halted"
        elif state in {"STALE", "OFFLINE"}:
            engine_class = "stale"
        else:
            engine_class = "active"
        st.markdown(
            f'<span class="status-badge {engine_class}">ENGINE · {state}</span>',
            unsafe_allow_html=True,
        )
    with status_cols[2]:
        st.caption(datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%H:%M:%S IST"))
        render_theme_studio()
    with status_cols[3]:
        asset = st.session_state.get("selected_asset")
        quote = snapshot.quote or {}
        if quote.get("valid"):
            digits = int(quote.get("digits") or 5)
            st.caption(
                f"{asset}  ·  BID {quote['bid']:.{digits}f}  ·  "
                f"ASK {quote['ask']:.{digits}f}  ·  SPREAD {quote['spread']} pts"
            )
        elif asset:
            st.caption(f"{asset}  ·  {quote.get('reason') or 'Quote unavailable'}")
        else:
            st.caption("Market quote · —")
        next_event = next_upcoming_high_impact(snapshot.news.events)
        if next_event:
            st.markdown(
                f'<span class="news-countdown">{countdown_text(next_event)}</span>',
                unsafe_allow_html=True,
            )


def _draw_metrics(snapshot) -> None:
    account = snapshot.account
    positions = snapshot.positions
    currency = account.currency or "—"
    open_risk = account.open_risk
    if open_risk is None:
        risk_value = "—"
    elif not math.isfinite(open_risk):
        risk_value = "UNBOUNDED"
    else:
        risk_value = format_money(open_risk, currency)
    equity = account.equity
    if open_risk is not None and math.isfinite(open_risk) and equity and equity > 0:
        risk_pct = f"{open_risk / equity * 100:.2f}%"
    elif open_risk is not None and not math.isfinite(open_risk):
        risk_pct = "UNBOUNDED"
    else:
        risk_pct = "—"
    values = (
        ("Balance", format_money(account.balance, currency)),
        ("Floating equity", format_money(account.equity, currency)),
        ("Margin used", format_money(account.margin_used, currency)),
        ("Free margin", format_money(account.free_margin, currency)),
        ("Margin level", format_margin_level(account.margin_used, account.margin_level, currency)),
        ("Open P&L", format_money(account.open_pnl, currency)),
        ("Open risk", risk_value),
        ("Open positions", str(len(positions)) if account.connected else "—"),
    )
    metric_cols = st.columns(8, gap="small")
    for column, (label, value) in zip(metric_cols, values):
        accent = _METRIC_ACCENTS[label]
        with column:
            with accent_card(accent, label.lower().replace(" ", "-")):
                if label == "Open risk":
                    missing = tickets_without_stop(positions)
                    if open_risk is not None and not math.isfinite(open_risk):
                        st.metric(f"Open risk · {currency}", value)
                        st.caption(f"{len(missing)} positions without SL")
                    else:
                        st.metric(
                            f"Open risk · {currency}",
                            value,
                            delta=f"{risk_pct} of equity",
                            delta_color="off",
                        )
                else:
                    st.metric(label, value)
