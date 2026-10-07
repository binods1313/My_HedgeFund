from __future__ import annotations

import streamlit as st

from data_engine import TIMEFRAME_D1, get_symbol_live_quote, get_technical_dataframe
from ui.data_access import get_account_snapshot, get_active_positions, get_equity_history, get_recent_events


@st.cache_data(ttl=1, show_spinner=False)
def account_snapshot(_bucket: int):
    return get_account_snapshot()


@st.cache_data(ttl=1, show_spinner=False)
def active_positions(_bucket: int):
    return get_active_positions()


@st.cache_data(ttl=2, show_spinner=False)
def live_quote(asset: str, _bucket: int):
    try:
        return get_symbol_live_quote(asset)
    except Exception:
        return {"valid": False, "reason": "Quote unavailable"}


@st.cache_data(ttl=2, show_spinner=False)
def chart_data(asset: str, timeframe: int, count: int, _bucket: int):
    try:
        return get_technical_dataframe(asset, timeframe, count=count)
    except Exception:
        return None


@st.cache_data(ttl=30, show_spinner=False)
def daily_closes(asset: str, _bucket: int):
    """Recent D1 closes, oldest first. None when the daily feed is unavailable."""
    try:
        frame = get_technical_dataframe(asset, TIMEFRAME_D1, count=5)
    except Exception:
        return None
    if frame is None or getattr(frame, "empty", True) or "close" not in frame:
        return None
    closes = []
    for value in frame["close"].tolist():
        try:
            closes.append(float(value))
        except (TypeError, ValueError):
            return None
    return closes


@st.cache_data(ttl=5, show_spinner=False)
def equity_history(_bucket: int, limit: int = 200):
    return get_equity_history(limit)


@st.cache_data(ttl=5, show_spinner=False)
def recent_events(_bucket: int, limit: int = 100):
    return get_recent_events(limit)