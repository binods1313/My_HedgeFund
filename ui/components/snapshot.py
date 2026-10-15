"""One broker snapshot per Streamlit run or fragment tick.

Components must read this object instead of calling the broker again.
Two calls in the same run return the same ``RunSnapshot`` instance.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pandas as pd
import streamlit as st

from ui.components.data_cache import daily_closes, live_quote, recent_events
from ui.indicators import change_24h_label
from ui.data_access import AccountSnapshot, get_account_snapshot, get_active_positions
from ui.services.news import FetchResult, fetch_news

_CACHE: dict[str, "RunSnapshot"] = {}


@dataclass(frozen=True)
class RunSnapshot:
    account: AccountSnapshot
    positions: pd.DataFrame
    events: pd.DataFrame
    news: FetchResult
    quote: dict


def load_shared_snapshot(*, refresh: bool = False) -> RunSnapshot:
    """Return the run's snapshot, fetching only when ``refresh`` is set or the cache is empty."""
    token = st.session_state.get("_snapshot_token")
    if not refresh and token in _CACHE:
        return _CACHE[token]

    token = uuid.uuid4().hex
    account = get_account_snapshot()
    positions = get_active_positions()
    try:
        events = recent_events(0, 100)
    except Exception:
        events = pd.DataFrame(
            columns=["id", "ts", "category", "severity", "source", "message", "context_json"]
        )
    news = fetch_news()
    asset = st.session_state.get("selected_asset")
    quote = dict(live_quote(asset, 0) if asset else {"valid": False, "reason": "No symbol selected"})
    current = None
    if quote.get("valid") and quote.get("bid") is not None and quote.get("ask") is not None:
        current = (float(quote["bid"]) + float(quote["ask"])) / 2.0
    quote["change_24h"] = change_24h_label(daily_closes(asset, 0) if asset else None, current)
    snapshot = RunSnapshot(account, positions, events, news, quote)
    _CACHE.clear()
    _CACHE[token] = snapshot
    st.session_state["_snapshot_token"] = token
    st.session_state["account"] = account
    st.session_state["_active_positions"] = positions
    st.session_state["_run_snapshot"] = snapshot
    return snapshot


def tickets_without_stop(positions: pd.DataFrame | None) -> list[int]:
    """Tickets whose stop is missing or zero. Display-only; it does not score risk."""
    if positions is None or getattr(positions, "empty", True):
        return []
    tickets: list[int] = []
    for row in positions.to_dict("records"):
        stop = row.get("sl")
        unprotected = stop is None or stop == "" or (
            isinstance(stop, (int, float)) and not isinstance(stop, bool) and float(stop) == 0.0
        )
        ticket = row.get("ticket")
        if not unprotected or ticket is None or ticket != ticket:
            continue
        try:
            tickets.append(int(ticket))
        except (TypeError, ValueError):
            continue
    return tickets
