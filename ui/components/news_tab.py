from __future__ import annotations

import html
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import streamlit as st

from ui.components.snapshot import RunSnapshot
from ui.services.news import NewsEvent, countdown_text, currencies_for_symbol
from ui.state import (
    persist_news_affects_open,
    persist_news_currencies,
    persist_news_impacts,
    persist_news_query,
    persist_news_show_past,
    persist_news_window,
)

DISPLAY_TIMEZONE = ZoneInfo("Asia/Kolkata")
IMPACT_OPTIONS = ["High", "Medium", "Low", "Holiday", "Unknown"]
WINDOW_OPTIONS = ("Today", "24h", "This week")


def render_news_tab(snapshot: RunSnapshot | None = None) -> None:
    st.subheader("Live market news")
    result = snapshot.news if snapshot is not None else None
    if result is None:
        from ui.services.news import fetch_news
        result = fetch_news()
    if result.status == "ok":
        st.session_state["last_good_news"] = (result.fetched_at, result.events)
    elif result.cached:
        st.warning(f"Showing cached data from {_local_time(result.fetched_at)} · source unreachable")
    else:
        last_good = st.session_state.get("last_good_news")
        if last_good:
            fetched_at, events = last_good
            from ui.services.news import FetchResult
            result = FetchResult(list(events), fetched_at, "cached", result.warnings, cached=True)
            st.warning(f"Showing cached data from {_local_time(fetched_at)} · source unreachable")
        else:
            st.warning("Calendar feed unavailable; no cached events are available.")

    asset = st.session_state.get("selected_asset")
    defaults = _default_currencies(asset)
    if st.session_state.get("_news_filter_asset") != asset:
        st.session_state["news_currencies"] = defaults
        st.session_state["w_news_currencies"] = defaults
        st.session_state["_news_filter_asset"] = asset

    known = {event.currency for event in result.events}
    currency_options = sorted((known | set(defaults)) - {""})
    if "ALL" not in currency_options:
        currency_options.append("ALL")

    chosen = [item for item in (st.session_state.get("w_news_currencies") or []) if item in currency_options]
    st.session_state["w_news_currencies"] = chosen or defaults
    impacts = st.pills(
        "Impact",
        IMPACT_OPTIONS,
        selection_mode="multi",
        key="w_news_impacts",
        on_change=persist_news_impacts,
    ) or []
    filter_cols = st.columns([1.6, 1.2, 1], gap="small")
    with filter_cols[0]:
        currencies = st.multiselect(
            "Currencies",
            currency_options,
            key="w_news_currencies",
            on_change=persist_news_currencies,
        )
    with filter_cols[1]:
        query = st.text_input("Search events", key="w_news_query", on_change=persist_news_query)
    with filter_cols[2]:
        window = st.segmented_control(
            "Time window",
            list(WINDOW_OPTIONS),
            key="w_news_window",
            on_change=persist_news_window,
        ) or "Today"
    toggle_cols = st.columns(2, gap="small")
    with toggle_cols[0]:
        affects = st.toggle(
            "Affects my open positions",
            key="w_news_affects_open",
            on_change=persist_news_affects_open,
        )
    with toggle_cols[1]:
        show_past = st.toggle("Show past", key="w_news_show_past", on_change=persist_news_show_past)

    positions = snapshot.positions if snapshot is not None else None
    open_symbols = []
    if positions is not None and not getattr(positions, "empty", True) and "symbol" in positions.columns:
        open_symbols = [str(symbol) for symbol in positions["symbol"].dropna().tolist()]

    filtered = filter_events(
        result.events,
        currencies=set(currencies or []),
        impacts=set(impacts or []),
        query=str(query or ""),
        window=str(window or "Today"),
        show_past=bool(show_past),
        open_symbols=open_symbols if affects else None,
    )
    if result.cached or result.status != "ok":
        st.caption(f"cached data from {_local_time(result.fetched_at)}")
    if not filtered and not result.events:
        st.info("No calendar events match the current filters.")
        return
    st.html(build_news_html(filtered, open_symbols=open_symbols, now=datetime.now(timezone.utc)))
    if not filtered:
        st.info("No calendar events match the current filters.")


def _default_currencies(asset: str | None) -> list[str]:
    currencies = currencies_for_symbol(asset)
    if "ALL" not in currencies:
        currencies = [*currencies, "ALL"]
    return currencies


def filter_events(
    events: list[NewsEvent],
    *,
    currencies: set[str],
    impacts: set[str],
    query: str,
    window: str,
    show_past: bool,
    open_symbols: list[str] | None,
    now: datetime | None = None,
) -> list[NewsEvent]:
    clock = now or datetime.now(timezone.utc)
    today = clock.astimezone(DISPLAY_TIMEZONE).date()
    selected_currencies = {value.upper() for value in currencies}
    needle = query.strip().casefold()
    open_currencies = set()
    if open_symbols is not None:
        for symbol in open_symbols:
            open_currencies.update(code.upper() for code in currencies_for_symbol(symbol))
    kept: list[NewsEvent] = []
    for event in events:
        if impacts and event.impact not in impacts:
            continue
        code = event.currency.upper()
        if selected_currencies and code != "ALL" and code not in selected_currencies:
            continue
        if needle and needle not in event.title.casefold() and needle not in code.casefold():
            continue
        if open_symbols is not None and code != "ALL" and code not in open_currencies:
            continue
        day = _event_day(event)
        if not _in_window(event, day, window, today, clock):
            continue
        if not show_past and day is not None and day < today:
            continue
        kept.append(event)
    kept.sort(key=lambda event: (_event_day(event) or date.max, event.when_utc or datetime.max.replace(tzinfo=timezone.utc), event.title))
    return kept


def _in_window(event: NewsEvent, day: date | None, window: str, today: date, now: datetime) -> bool:
    if window == "This week":
        return True
    if window == "24h":
        if event.when_utc is None:
            return day in {today, today + timedelta(days=1)}
        stamp = event.when_utc if event.when_utc.tzinfo else event.when_utc.replace(tzinfo=timezone.utc)
        return now - timedelta(hours=1) <= stamp <= now + timedelta(hours=24)
    return day == today or day is None


def build_news_html(
    events: list[NewsEvent],
    *,
    open_symbols: list[str] | None = None,
    now: datetime | None = None,
) -> str:
    """Render the news grid. Every dynamic string is escaped."""
    clock = now or datetime.now(timezone.utc)
    symbols = open_symbols or []
    high = [event for event in events if event.impact == "High"]
    medium = [event for event in events if event.impact == "Medium"]
    low = [event for event in events if event.impact in {"Low", "Holiday", "Unknown"}]
    next_high = next((event for event in high if event.when_utc and _as_utc(event.when_utc) >= clock), None)
    countdown = html.escape(countdown_text(next_high, clock) if next_high else "—")
    days = _weekday_counts(events, clock)
    day_html = "".join(
        f'<div class="news-day"><span class="news-kicker">{html.escape(name)}</span>'
        f'<div>{html.escape(label)}</div></div>'
        for name, label in days
    )
    cards = "".join(_event_card(event, symbols, clock, expanded=True) for event in high)
    cards += "".join(_event_card(event, symbols, clock, expanded=False) for event in medium)
    low_rows = "".join(
        f'<p class="news-chip">{html.escape(event.impact)} · {html.escape(event.currency or "—")} · {html.escape(event.title)}</p>'
        for event in low
    )
    low_block = (
        '<details class="news-low"><summary>Low-impact & holidays</summary>'
        + (low_rows or '<p class="news-chip">—</p>')
        + "</details>"
    ) if low else ""
    grid = cards or '<p class="hf-empty">No high or medium events in this window.</p>'
    return (
        '<section class="news-strip"><div><p class="news-kicker">NOW / NEXT 24h</p>'
        f'<h2>{countdown}</h2></div>{day_html}</section>'
        f'<div class="news-grid">{grid}</div>{low_block}'
    )


def why_it_matters(event: NewsEvent, open_symbols: list[str]) -> str:
    code = (event.currency or "").upper()
    matched = [symbol for symbol in open_symbols if code == "ALL" or code in {item.upper() for item in currencies_for_symbol(symbol)}]
    if matched:
        return f"why it matters for {', '.join(matched)}"
    if open_symbols:
        return f"why it matters for open pairs: {code or '—'} does not match"
    return "why it matters for open pairs: no open positions"


def _event_card(event: NewsEvent, open_symbols: list[str], now: datetime, *, expanded: bool) -> str:
    kind = "high" if expanded else "medium"
    title = html.escape(event.title or "—")
    currency = html.escape(event.currency or "—")
    when = html.escape(_clock_label(event))
    countdown = html.escape(countdown_text(event, now) if event.when_utc else "—")
    forecast = html.escape(event.forecast or "—")
    previous = html.escape(event.previous or "—")
    actual = html.escape(getattr(event, "actual", None) or "—")
    why = html.escape(why_it_matters(event, open_symbols))
    if expanded:
        body = (
            f'<p class="news-kicker">{currency} · HIGH</p><h3>{title}</h3>'
            f'<p>{when} · {countdown}</p>'
            f'<p>Forecast {forecast} · Previous {previous} · Actual {actual}</p>'
            f'<p>{why}</p>'
        )
    else:
        body = f'<p class="news-kicker">{currency} · MEDIUM</p><h3>{title}</h3><p>{countdown}</p>'
    details = (
        "<details><summary>Full row</summary>"
        f"<p>{currency}</p><p>{title}</p><p>Forecast {forecast}</p><p>Previous {previous}</p>"
        f"<p>Actual {actual}</p><p>{why}</p></details>"
    )
    return f'<article class="news-card {kind}">{body}{details}</article>'


def _weekday_counts(events: list[NewsEvent], now: datetime) -> list[tuple[str, str]]:
    start = now.astimezone(DISPLAY_TIMEZONE).date()
    monday = start - timedelta(days=start.weekday())
    days = [monday + timedelta(days=offset) for offset in range(5)]
    counts: dict[date, Counter] = {day: Counter() for day in days}
    for event in events:
        day = _event_day(event)
        if day in counts:
            counts[day][event.impact] += 1
    labels = []
    for day in days:
        counter = counts[day]
        high = counter.get("High", 0)
        medium = counter.get("Medium", 0)
        labels.append((day.strftime("%a"), f"{high} High · {medium} Medium"))
    return labels


def _event_day(event: NewsEvent) -> date | None:
    if event.when_utc is not None:
        return _as_utc(event.when_utc).astimezone(DISPLAY_TIMEZONE).date()
    return event.calendar_date


def _clock_label(event: NewsEvent) -> str:
    if event.when_utc is None:
        return "Time unspecified"
    return _as_utc(event.when_utc).astimezone(DISPLAY_TIMEZONE).strftime("%H:%M IST")


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _local_time(value: datetime) -> str:
    local_value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return local_value.astimezone(DISPLAY_TIMEZONE).strftime("%H:%M IST")
