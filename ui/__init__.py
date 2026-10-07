"""UI layer for the trading dashboard.

This package holds the Streamlit-specific configuration and guardrails that keep
business logic importable and testable outside the app runtime.
"""

from .state import (
	DEFAULTS,
	init_state,
	persist_active_tab,
	persist_asset_category,
	persist_chart_style,
	persist_exposure_pct,
	persist_lot_size,
	persist_order_side,
	persist_order_type,
	persist_selected_asset,
	persist_sl_pips,
	persist_slippage_pips,
	persist_timeframe,
)
from .theme import TOKENS, inject_theme, plotly_theme

__all__ = [
	"DEFAULTS",
	"TOKENS",
	"init_state",
	"inject_theme",
	"persist_active_tab",
	"persist_asset_category",
	"persist_chart_style",
	"persist_exposure_pct",
	"persist_lot_size",
	"persist_order_side",
	"persist_order_type",
	"persist_selected_asset",
	"persist_sl_pips",
	"persist_slippage_pips",
	"persist_timeframe",
	"plotly_theme",
]
