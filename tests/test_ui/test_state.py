from ui.state import (
    DEFAULTS,
    init_state,
    persist_asset_category,
    persist_timeframe,
)


def test_defaults_and_validation() -> None:
    import streamlit as st

    st.session_state.clear()
    whitelist = {"forex": ["EURUSD", "GBPUSD"], "crypto": ["BTCUSD"]}

    init_state(whitelist)

    assert st.session_state["active_tab"] == DEFAULTS["active_tab"]
    assert st.session_state["asset_category"] == "forex"
    assert st.session_state["selected_asset"] == "EURUSD"

    st.session_state["asset_category"] = "crypto"
    init_state(whitelist)
    assert st.session_state["selected_asset"] == "BTCUSD"


def test_category_callback_resets_selected_symbol() -> None:
    import streamlit as st

    st.session_state.clear()
    whitelist = {"forex": ["EURUSD", "GBPUSD"], "crypto": ["BTCUSD"]}
    init_state(whitelist)
    st.session_state["selected_asset"] = "GBPUSD"
    st.session_state["w_asset_category"] = "crypto"

    persist_asset_category()

    assert st.session_state["asset_category"] == "crypto"
    assert st.session_state["selected_asset"] == "BTCUSD"
    assert st.session_state["w_selected_asset"] == "BTCUSD"


def test_mirror_restores_widget_value_after_widget_key_disappears() -> None:
    import streamlit as st

    st.session_state.clear()
    whitelist = {"forex": ["EURUSD"]}
    init_state(whitelist)
    st.session_state["w_timeframe"] = "4H"
    persist_timeframe()
    del st.session_state["w_timeframe"]

    init_state(whitelist)

    assert st.session_state["timeframe"] == "4H"
    assert st.session_state["w_timeframe"] == "4H"


def test_zero_lot_and_stop_widgets_follow_saved_mirrors() -> None:
    import streamlit as st

    st.session_state.clear()
    whitelist = {"forex": ["EURUSD"]}
    init_state(whitelist)
    st.session_state["lot_size"] = 0.01
    st.session_state["sl_pips"] = 30.0
    st.session_state["w_lot_size"] = 0.0
    st.session_state["w_sl_pips"] = 0.0

    init_state(whitelist)

    assert st.session_state["w_lot_size"] == st.session_state["lot_size"] == 0.01
    assert st.session_state["w_sl_pips"] == st.session_state["sl_pips"] == 30.0


def test_saved_stop_of_ten_pips_is_not_replaced() -> None:
    import streamlit as st

    st.session_state.clear()
    whitelist = {"forex": ["EURUSD"]}
    init_state(whitelist)
    st.session_state["sl_pips"] = 10.0
    st.session_state["w_sl_pips"] = 10.0

    init_state(whitelist)

    assert st.session_state["w_sl_pips"] == 10.0
    assert st.session_state["sl_pips"] == 10.0


def test_init_state_resets_asset_when_category_changes_even_if_symbol_overlaps() -> None:
    import streamlit as st

    st.session_state.clear()
    init_state({"forex": ["EURUSD"], "indices": ["EURUSD", "US500"]})
    st.session_state["asset_category"] = "indices"

    init_state({"forex": ["EURUSD"], "indices": ["US500", "EURUSD"]})

    assert st.session_state["selected_asset"] == "US500"
