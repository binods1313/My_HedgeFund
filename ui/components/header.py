from __future__ import annotations

import html
import math
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from formatting import format_margin_level, format_money
from ui.components.auxiliary_views import render_portfolio_view
from ui.components.cards import accent_card
from ui.components.enable_ai import render_enable_ai
from ui.components.emergency_stop import render_emergency_stop
from ui.components.nav import TAB_LABELS, render_navigation
from ui.components.snapshot import load_shared_snapshot, tickets_without_stop
from ui.components.theme_studio import render_theme_studio
from ui.services.news import header_chip_event, header_chip_text
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

_ROOT = Path(__file__).resolve().parents[2]


def _market_closed_now() -> bool:
    """True inside the configured FX/metals weekend closure.

    The same hours that block new entries decide when an old tick means a
    closed market rather than a dead feed. Unreadable config fails to the
    defaults in ``data_engine.trading_week_closed``.
    """
    try:
        import yaml
        raw = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
        sessions = raw.get("sessions") or {}
        friday = int(sessions.get("block_friday_after_utc_hour", 20))
        sunday = int(sessions.get("block_sunday_before_utc_hour", 21))
    except Exception:
        friday, sunday = 20, 21
    from data_engine import trading_week_closed
    return trading_week_closed(
        datetime.now(timezone.utc),
        friday_after_utc_hour=friday,
        sunday_before_utc_hour=sunday,
    )


def _quote_caption_text(asset: str, quote: dict, *, market_closed: bool) -> str:
    """Header quote line. A stale tick in a closed market is not an alarm."""
    reason = str(quote.get("reason") or "Quote unavailable")
    if market_closed and reason.startswith("Stale tick"):
        return f"{asset}  ·  Market closed"
    return f"{asset}  ·  {reason}"


def _scan_badge(status: dict) -> str:
    """The scan line the status card shows. Never the retired placeholder."""
    notice = status.get("schema_notice")
    if notice:
        return str(notice)
    if status.get("scan_loop"):
        return str(status["scan_loop"])
    return (
        "scan_running=" + str(status.get("scan_running"))
        + " last_cycle_ts=" + str(status.get("last_cycle_ts") or "—")
        + " symbols_scanned=" + str(status.get("symbols_scanned"))
    )


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
        mode = html.escape(str(account.mode or "—"))
        st.markdown(
            f'<span class="status-badge {connection_class}">{connection} · {mode}</span>',
            unsafe_allow_html=True,
        )
        with status_cols[1]:
            # Two distinct facts, deliberately not merged: whether the engine
            # process is alive (liveness) and what its trading state is
            # (`engine_state`). A heartbeating engine with trading OFF is
            # running; the old code collapsed both into one badge and showed
            # OFFLINE for a healthy engine.
            state = (account.engine_state or "offline").upper()
            age = account.heartbeat_age_s
            alive = bool(getattr(account, "engine_online", False))
            if not alive:
                label = "STALE" if age is not None else "OFFLINE"
            elif state in {"HALTED", "PAUSED", "EMERGENCY_STOP"}:
                label = state
            else:
                label = "ONLINE"
            if label in {"HALTED", "PAUSED", "EMERGENCY_STOP"}:
                engine_class = "halted"
            elif label in {"STALE", "OFFLINE"}:
                engine_class = "stale"
            else:
                engine_class = "active"
            st.markdown(
                f'<span class="status-badge {engine_class}">ENGINE · '
                f'{html.escape(label)}</span>',
                unsafe_allow_html=True,
            )
        render_enable_ai(account.mode)
        render_emergency_stop(account.mode, account.engine_state)

    # -- system status card (A3, honest wording) -----------------------------
    try:
        from ui.data_access import system_status

        status = system_status()
    except Exception:
        status = {}

    def _badge(value, color, empty="—"):
        text = "" if value is None else str(value)
        if text == "":
            text = empty
        return f'<span class="status-badge {color}">{html.escape(text)}</span>'

    age = account.heartbeat_age_s
    alive = bool(getattr(account, "engine_online", False))
    last = status.get("last_trade")
    if isinstance(last, dict):
        last_text = f"{last.get('side')} {last.get('symbol')} {last.get('volume')} · {last.get('status')}"
    else:
        last_text = "No trade yet"
    trades_today = status.get("trades_today")
    trades_text = "unknown (journal not read)" if trades_today is None else str(trades_today)
    st.markdown('<div class="hf-line2">', unsafe_allow_html=True)
    with status_cols[0]:
        st.markdown('<div class="hf-label">Status card · A3</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="status-grid">'
            + _badge(status.get("engine_state") or "no state yet", "active")
            + _badge(status.get("account_currency"), "cyan")
            + _badge(status.get("account_login_masked"), "green")
            + _badge(last_text, "cyan")
            + _badge("trades today " + trades_text, "green")
            + _badge(_scan_badge(status), "cyan")
            + _badge("execution_mode=" + str(status.get("execution_mode") or "approve_first"), "cyan")
            + _badge(str(status.get("signals", "No signals yet")), "cyan")
            + '</div>'
            '<div class="status-shimmer">Heartbeat ' + str(age) + "s"
            + ' · ' + ("running" if alive else "stale") + ' · mode ' + str(status.get("mode") or "unknown")
            + '</div>'
            '<div class="status-shimmer">Guard posture · '
            + ("verified" if alive else "TerminalOffline/WARNING · writes throttled")
            + '</div>'
            '<div class="status-shimmer">PAPER fills are simulated. They do not prove MT5 execution, fills or broker stop rules.</div>',
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)
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
            st.caption(_quote_caption_text(asset, quote, market_closed=_market_closed_now()))
        else:
            st.caption("Market quote · —")
        events = snapshot.news.events
        chip = html.escape(header_chip_text(events))
        # The empty state is information, not an alarm: calm styling. The red
        # treatment is reserved for an actual high-impact event in the window.
        chip_class = " calm" if header_chip_event(events) is None else ""
        st.markdown(
            f'<span class="news-countdown{chip_class}">{chip}</span>',
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
