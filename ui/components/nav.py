"""Main-module navigation and click-to-open fact popovers.

Streamlit has no hover menus. Each item is a button plus an st.popover.
Facts come from the shared run snapshot and are never fetched here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import streamlit as st

from formatting import format_money
from ui.components.cards import relative_time
from ui.components.snapshot import RunSnapshot, tickets_without_stop
from ui.services.news import countdown_text, next_upcoming_high_impact
from ui.state import persist_active_tab

TAB_LABELS = (
    "🌐 Exchange",
    "💼 Portfolio",
    "🤖 AI Signals",
    "📰 Live Market News",
    "🧾 Journal",
)

_SLUGS = ("exchange", "portfolio", "signals", "news", "journal")
_IST = ZoneInfo("Asia/Kolkata")


def render_navigation(snapshot: RunSnapshot) -> str:
    active = st.session_state.get("active_tab", TAB_LABELS[0])
    if active not in TAB_LABELS:
        active = TAB_LABELS[0]
    st.session_state["w_active_tab"] = active
    columns = st.columns(len(TAB_LABELS), gap="small")
    for column, label, slug in zip(columns, TAB_LABELS, _SLUGS):
        with column:
            label_col, pop_col = st.columns([5, 1], gap="small", vertical_alignment="center")
            with label_col:
                st.button(
                    label,
                    key=f"nav-{slug}" + ("-on" if label == active else ""),
                    type="secondary",
                    width="stretch",
                    on_click=_activate,
                    args=(label,),
                )
                if label == active:
                    st.html(
                        f'<span class="hf-nav-current" data-nav="{slug}" aria-current="page" aria-selected="true"></span>'
                    )
            with pop_col:
                with st.popover("▾", help=f"Quick facts for {label}", key=f"pop-{slug}"):
                    _render_facts(slug, snapshot)
                    st.button(
                        f"Open {label}",
                        key=f"open-{slug}",
                        width="stretch",
                        on_click=_activate,
                        args=(label,),
                    )
    return active


def _activate(label: str) -> None:
    st.session_state["w_active_tab"] = label
    persist_active_tab()
    st.session_state["active_tab"] = label


def _render_facts(slug: str, snapshot: RunSnapshot) -> None:
    if slug == "exchange":
        _exchange_facts(snapshot)
    elif slug == "portfolio":
        _portfolio_facts(snapshot)
    elif slug == "signals":
        _signal_facts(snapshot)
    elif slug == "news":
        _news_facts(snapshot)
    else:
        _journal_facts(snapshot)


def _line(label: str, value: object) -> None:
    text = "—" if value is None or value == "" else str(value)
    st.markdown(f"**{label}**  \n{text}")


def _exchange_facts(snapshot: RunSnapshot) -> None:
    quote = snapshot.quote or {}
    asset = st.session_state.get("selected_asset") or "—"
    if quote.get("valid"):
        digits = int(quote.get("digits") or 5)
        _line("Symbol", asset)
        _line("Bid", f"{quote['bid']:.{digits}f}")
        _line("Ask", f"{quote['ask']:.{digits}f}")
        _line("Spread", f"{quote.get('spread')} pts")
    else:
        _line("Symbol", asset)
        _line("Bid / ask / spread", "—")
    change = quote.get("change_24h")
    _line("24h change", "—" if change in (None, "") else change)
    age = quote.get("tick_age_s")
    _line("Data age", f"{age}s" if age is not None else "—")
    event = next_upcoming_high_impact(snapshot.news.events)
    _line("Next high-impact event", countdown_text(event) if event else "—")


def _portfolio_facts(snapshot: RunSnapshot) -> None:
    account = snapshot.account
    currency = account.currency or "—"
    _line("Balance", format_money(account.balance, currency))
    _line("Equity", format_money(account.equity, currency))
    _line("Open P&L", format_money(account.open_pnl, currency))
    _line("Top exposure", _top_exposure(snapshot))
    missing = tickets_without_stop(snapshot.positions)
    _line("Positions without SL", str(len(missing)) if account.connected else "—")


def _signal_facts(snapshot: RunSnapshot) -> None:
    state = st.session_state.get("ai_state") or {}
    symbol = state.get("symbol") or "—"
    direction = state.get("decision") or "—"
    confidence = state.get("confidence")
    _line("Last signal", f"{symbol} {direction}")
    _line("Confidence", "—" if confidence is None else confidence)
    _line("Age", state.get("timestamp") or "—")
    _line("Validator verdict", state.get("gate_label") or "—")
    _line("Engine state", (snapshot.account.engine_state or "—").upper())


def _news_facts(snapshot: RunSnapshot) -> None:
    now_events = [
        event for event in snapshot.news.events
        if event.impact == "High" and event.when_utc is not None
    ]
    now_events.sort(key=lambda event: event.when_utc)
    upcoming = [event for event in now_events if event.when_utc >= datetime.now(timezone.utc)][:3]
    if not upcoming:
        _line("Next high-impact events", "—")
        return
    for index, event in enumerate(upcoming, start=1):
        _line(f"High {index}", countdown_text(event))


def _journal_facts(snapshot: RunSnapshot) -> None:
    frame = snapshot.events
    if frame is None or frame.empty:
        _line("Last events", "—")
        _line("WARN/ERROR today", "0")
        return
    records = frame.to_dict("records")[:5]
    for index, record in enumerate(records, start=1):
        severity = str(record.get("severity") or "INFO").upper()
        message = str(record.get("message") or "—")
        when = relative_time(record.get("ts"), datetime.now(timezone.utc))
        _line(f"{index}", f"{severity} · {when} · {message}")
    today = datetime.now(_IST).date()
    count = 0
    for record in frame.to_dict("records"):
        severity = str(record.get("severity") or "").upper()
        if severity not in {"WARN", "WARNING", "ERROR"}:
            continue
        parsed = record.get("ts")
        try:
            stamp = datetime.fromisoformat(str(parsed).replace("Z", "+00:00"))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp.astimezone(_IST).date() == today:
            count += 1
    _line("WARN/ERROR today", str(count))


def _top_exposure(snapshot: RunSnapshot) -> str:
    frame = snapshot.positions
    if frame is None or frame.empty or "symbol" not in frame.columns:
        return "—"
    totals: dict[str, float] = {}
    for row in frame.to_dict("records"):
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        try:
            lots = abs(float(row.get("lots") or 0.0))
        except (TypeError, ValueError):
            lots = 0.0
        totals[symbol] = totals.get(symbol, 0.0) + lots
    if not totals:
        return "—"
    symbol, lots = max(totals.items(), key=lambda item: item[1])
    return f"{symbol} · {lots:.2f} lots"
