from __future__ import annotations

import time

import streamlit as st

from config_loader import load_config
from data_engine import get_symbol_live_quote
from engine.config import load_engine_config
from ui.components.data_cache import live_quote
from ui.data_access import AccountSnapshot, get_required_margin, get_symbol_spec
from ui.components.snapshot import tickets_without_stop
from ui.risk_calc import ValidationFailure, ValidationReport, normalize_lots, validate_pre_submit


def inspect_order(
    *,
    asset: str,
    category: str,
    direction: str,
    lots: float,
    stop_units: float,
    slippage: float,
    whitelist: dict[str, list[str]],
    order_token: str | None = None,
) -> tuple[ValidationReport, dict]:
    account = st.session_state.get("account")
    if not isinstance(account, AccountSnapshot):
        from ui.data_access import get_account_snapshot
        account = get_account_snapshot()
        st.session_state["account"] = account

    bucket = int(time.time())
    quote = live_quote(asset, bucket) if asset else {"valid": False, "reason": "No symbol selected"}
    spec = get_symbol_spec(asset, category) if asset else None
    try:
        risk_config = load_engine_config().risk
        ui_config = load_config()
    except Exception:
        report = ValidationReport((ValidationFailure("CONFIG_INVALID", "Risk configuration is unavailable"),))
        return report, {"account": account, "quote": quote, "spec": spec, "lots": None, "required": None}

    normalized_lots = None
    if spec is not None and lots > 0:
        try:
            normalized_lots = normalize_lots(lots, spec)
        except (TypeError, ValueError, OverflowError):
            normalized_lots = None
    required = (
        get_required_margin(asset, direction, normalized_lots)
        if normalized_lots is not None and normalized_lots > 0
        else None
    )
    spread_limit = risk_config.default_max_spread_points
    override = ui_config.symbols.overrides.get(asset, {})
    spread_limit = override.get("max_spread_points", spread_limit)
    report = validate_pre_submit(
        spec=spec,
        symbol=asset,
        lots=lots,
        stop_units=stop_units,
        equity=account.equity,
        required=required,
        free_margin=account.free_margin,
        margin_used=account.margin_used,
        engine_state=account.engine_state,
        connected=account.connected,
        heartbeat_age_s=account.heartbeat_age_s,
        require_engine_ready=False,
        whitelist=(symbol for symbols in whitelist.values() for symbol in symbols),
        market_open=quote.get("valid") is True,
        tick_fresh=quote.get("valid") is True,
        open_risk=account.open_risk if account.open_risk is not None else float("inf"),
        max_risk_per_trade_pct=risk_config.risk_per_trade_pct,
        max_total_risk_pct=risk_config.max_total_exposure_pct,
        margin_buffer=0.05,
        warn_level=100.0,
        spread_points=quote.get("spread") if quote.get("valid") else None,
        max_spread_points=spread_limit,
        slippage=slippage,
        order_token=order_token,
        used_tokens=[st.session_state.get("last_order_token")],
        unprotected_tickets=tickets_without_stop(st.session_state.get("_active_positions")),
    )
    return report, {
        "account": account,
        "quote": quote,
        "spec": spec,
        "lots": normalized_lots,
        "required": required,
    }


def execution_stop_units(spec, value: float) -> float:
    """The legacy execution adapter multiplies every SL input by ten points."""
    return value if spec and spec.category == "forex" else value / 10.0