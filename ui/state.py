from __future__ import annotations

import copy
from typing import Any

import streamlit as st

DEFAULTS: dict[str, Any] = {
    "active_tab": "🌐 Exchange",
    "asset_category": "Forex",
    "selected_asset": None,
    "chart_style": "Candlestick",
    "timeframe": "1H",
    "order_side": "BUY",
    "order_type": "Market",
    "exposure_pct": 0,
    "slippage_pips": 1.0,
    "lot_size": 0.01,
    "sl_pips": 30.0,
    "pending_close_ticket": None,
    "last_order_token": None,
    "last_good_news": None,
    "account": None,
    "news_currencies": [],
    "news_impacts": ["High", "Medium"],
    "news_query": "",
    "news_window": "Today",
    "news_show_past": False,
    "news_affects_open": False,
}

_WIDGET_TO_STATE = {
    "w_active_tab": "active_tab",
    "w_asset_category": "asset_category",
    "w_selected_asset": "selected_asset",
    "w_chart_style": "chart_style",
    "w_timeframe": "timeframe",
    "w_order_side": "order_side",
    "w_order_type": "order_type",
    "w_exposure_pct": "exposure_pct",
    "w_slippage_pips": "slippage_pips",
    "w_lot_size": "lot_size",
    "w_sl_pips": "sl_pips",
    "w_news_currencies": "news_currencies",
    "w_news_impacts": "news_impacts",
    "w_news_query": "news_query",
    "w_news_window": "news_window",
    "w_news_show_past": "news_show_past",
    "w_news_affects_open": "news_affects_open",
}


def init_state(whitelist: dict[str, list[str]] | None = None) -> None:
    """Initialize persistent state and restore widget keys after hidden runs.

    If a whitelist is provided, asset_category and selected_asset are validated
    against it. When no whitelist is supplied (e.g. broker unavailable at init),
    the defaults stand and the UI will gracefully fall back to the static
    configuration loaded on-demand.
    """
    for key, value in DEFAULTS.items():
        st.session_state.setdefault(key, copy.deepcopy(value))

    # Bind to dynamic broker list when available
    if whitelist is not None:
        st.session_state["_ui_whitelist"] = copy.deepcopy(whitelist)
        categories = [category for category, symbols in whitelist.items() if symbols]
        if not categories:
            st.session_state["asset_category"] = ""
            st.session_state["selected_asset"] = None
            st.session_state["_validated_asset_category"] = ""
            st.session_state["w_asset_category"] = ""
            st.session_state["w_selected_asset"] = None
            for widget_key, state_key in _WIDGET_TO_STATE.items():
                st.session_state.setdefault(widget_key, st.session_state[state_key])
            _seed_order_and_news_defaults()
            _sync_zero_widgets_from_mirrors()
            return

        previous_category = st.session_state.get("_validated_asset_category")
        category = st.session_state.get("asset_category")
        if category not in categories:
            category = categories[0]
            st.session_state["asset_category"] = category

        symbols = whitelist[category]
        selected = st.session_state.get("selected_asset")
        if previous_category is not None and previous_category != category or selected not in symbols:
            st.session_state["selected_asset"] = symbols[0]
        st.session_state["_validated_asset_category"] = category

        _seed_order_and_news_defaults()
        for widget_key, state_key in _WIDGET_TO_STATE.items():
            st.session_state.setdefault(widget_key, st.session_state[state_key])
        _sync_zero_widgets_from_mirrors()
        if st.session_state.get("w_asset_category") != category:
            st.session_state["w_asset_category"] = category
        if st.session_state.get("w_selected_asset") not in symbols or (
            previous_category is not None and previous_category != category
        ):
            st.session_state["w_selected_asset"] = st.session_state["selected_asset"]
    else:
        # No whitelist supplied at init - leave defaults as-is; UI will fetch
        # live broker list on-demand and trigger re-initialization later.
        st.session_state["_ui_whitelist"] = {}
        _seed_order_and_news_defaults()
        for widget_key, state_key in _WIDGET_TO_STATE.items():
            st.session_state.setdefault(widget_key, st.session_state[state_key])
        _sync_zero_widgets_from_mirrors()


def widget_follows_mirror(widget_value: object, mirror_value: object) -> bool:
    """True when a zero or empty widget should display the saved mirror."""
    try:
        mirror = float(mirror_value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if mirror <= 0:
        return False
    if widget_value is None or widget_value == "":
        return True
    try:
        return float(widget_value) <= 0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True


def _sync_zero_widgets_from_mirrors() -> None:
    for widget_key, state_key in (("w_lot_size", "lot_size"), ("w_sl_pips", "sl_pips")):
        mirror = st.session_state.get(state_key)
        if widget_follows_mirror(st.session_state.get(widget_key), mirror):
            st.session_state[widget_key] = float(mirror)


def _persist(widget_key: str, state_key: str) -> None:
    if widget_key in st.session_state:
        st.session_state[state_key] = st.session_state[widget_key]


def persist_active_tab() -> None:
    _persist("w_active_tab", "active_tab")


def persist_asset_category() -> None:
    """Mirror the chosen category and reset its symbol to a valid first choice."""
    whitelist = st.session_state.get("_ui_whitelist", {})
    category = st.session_state.get("w_asset_category")
    if category not in whitelist or not whitelist[category]:
        # Category changed or empty - try to refresh from live broker
        _refresh_broker_list_on_category_change()
        whitelist = st.session_state.get("_ui_whitelist", {})
        category = st.session_state.get("w_asset_category")
    if whitelist.get(category):
        st.session_state["asset_category"] = category
        st.session_state["selected_asset"] = whitelist[category][0]
        st.session_state["_validated_asset_category"] = category
        st.session_state["w_selected_asset"] = whitelist[category][0]


def persist_selected_asset() -> None:
    whitelist = st.session_state.get("_ui_whitelist", {})
    category = st.session_state.get("asset_category")
    selected = st.session_state.get("w_selected_asset")
    if selected in whitelist.get(category, []):
        st.session_state["selected_asset"] = selected
    elif not whitelist.get(category):
        # Whitelist empty - try refreshing from live broker
        _refresh_broker_list_on_category_change()
        whitelist = st.session_state.get("_ui_whitelist", {})
        category = st.session_state.get("asset_category")
        if selected in whitelist.get(category, []):
            st.session_state["selected_asset"] = selected


def persist_chart_style() -> None:
    _persist("w_chart_style", "chart_style")


def persist_timeframe() -> None:
    _persist("w_timeframe", "timeframe")


def persist_order_side() -> None:
    _persist("w_order_side", "order_side")


def persist_order_type() -> None:
    _persist("w_order_type", "order_type")


def persist_exposure_pct() -> None:
    _persist("w_exposure_pct", "exposure_pct")


def persist_slippage_pips() -> None:
    _persist("w_slippage_pips", "slippage_pips")


def persist_lot_size() -> None:
    _persist("w_lot_size", "lot_size")


def persist_sl_pips() -> None:
    _persist("w_sl_pips", "sl_pips")


def persist_news_currencies() -> None:
    _persist("w_news_currencies", "news_currencies")


def persist_news_impacts() -> None:
    _persist("w_news_impacts", "news_impacts")


def persist_news_query() -> None:
    _persist("w_news_query", "news_query")


def persist_news_window() -> None:
    _persist("w_news_window", "news_window")


def persist_news_show_past() -> None:
    _persist("w_news_show_past", "news_show_past")


def persist_news_affects_open() -> None:
    _persist("w_news_affects_open", "news_affects_open")


def _seed_order_and_news_defaults() -> None:
    """Restore broken 0.00 order defaults and the news High+Medium filter once per session."""
    if float(st.session_state.get("lot_size") or 0) <= 0:
        st.session_state["lot_size"] = 0.01
        st.session_state["w_lot_size"] = 0.01
    if float(st.session_state.get("sl_pips") or 0) <= 0:
        st.session_state["sl_pips"] = 30.0
        st.session_state["w_sl_pips"] = 30.0
    if not st.session_state.get("_news_defaults_v2"):
        asset = st.session_state.get("selected_asset")
        from ui.services.news import currencies_for_symbol
        currencies = currencies_for_symbol(asset)
        if "ALL" not in currencies:
            currencies = [*currencies, "ALL"]
        st.session_state["news_currencies"] = currencies
        st.session_state["w_news_currencies"] = list(currencies)
        st.session_state["news_impacts"] = ["High", "Medium"]
        st.session_state["w_news_impacts"] = ["High", "Medium"]
        st.session_state["news_window"] = st.session_state.get("news_window") or "Today"
        st.session_state["w_news_window"] = st.session_state["news_window"]
        st.session_state["news_show_past"] = bool(st.session_state.get("news_show_past", False))
        st.session_state["_news_defaults_v2"] = True


def _refresh_broker_list_on_category_change() -> None:
    """Callback: when the asset category changes, pull the live broker list
    and reset selected_asset to the first valid tradeable asset in that category."""
    from ui.data_access import fetch_live_broker_symbols

    # Try to get the live broker list; if it fails, fall back to empty (UI will show disabled)
    live_list = fetch_live_broker_symbols()
    category = st.session_state.get("asset_category", "")

    if not live_list:
        # Broker offline - keep whatever we have; UI should disable inputs
        st.session_state["_ui_whitelist"] = {}
        return

    # Use the live list as the new whitelist for this session
    # Re-initialize state against the new list
    init_state(live_list)
