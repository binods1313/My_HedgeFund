from __future__ import annotations

import math
import uuid

import streamlit as st

from formatting import format_money
from ui.components.cards import accent_card
from ui.components.trading import execution_stop_units, inspect_order
from ui.data_access import submit_order
from ui.risk_calc import normalize_lots, risk_card_accent, risk_summary_level, trade_risk
from ui.services.news import countdown_text, currencies_for_symbol, fetch_news, high_impact_within
from ui.order_types import CONDITIONAL_HELP, LIMIT_HELP, MARKET_HELP, UNSUPPORTED
from ui.state import (
    persist_lot_size,
    persist_exposure_pct,
    persist_order_side,
    persist_order_type,
    persist_sl_pips,
    persist_slippage_pips,
    widget_follows_mirror,
)


@st.dialog("Confirm order", width="small")
def _confirm_order_dialog() -> None:
    pending = st.session_state.get("_pending_order")
    if not pending:
        return

    st.write(
        f"{pending['direction']} {pending['lots']:.2f} lots of {pending['asset']}"
    )
    st.caption(
        f"Stop: {pending['stop_units']:g} {pending['stop_label']}  ·  "
        f"Target: {pending['tp_units']:g} {pending['stop_label']}"
    )
    st.caption(f"Estimated risk: {pending['risk_display']}  ·  Margin: {pending['margin_display']}")
    cancel_col, confirm_col = st.columns(2)
    with cancel_col:
        if st.button("Cancel", key="btn_cancel_order", width="stretch"):
            st.session_state["_pending_order"] = None
            st.rerun()
    with confirm_col:
        if st.button("Confirm", key="btn_confirm_order", type="primary", width="stretch"):
            report, context = inspect_order(
                asset=pending["asset"],
                category=pending["category"],
                direction=pending["direction"],
                lots=pending["lots"],
                stop_units=pending["stop_units"],
                slippage=pending["slippage"],
                whitelist=pending["whitelist"],
                submit_token=pending["token"],
            )
            if not report.ok:
                st.error("Order blocked by current checks")
                for failure in report.failures:
                    st.caption(f"{failure.code}: {failure.message}")
                return
            spec = context["spec"]
            if st.session_state.get("submit_in_flight"):
                return
            st.session_state["submit_in_flight"] = True
            st.session_state["last_order_token"] = pending["token"]
            try:
                result = submit_order(
                    pending["asset"],
                    pending["direction"],
                    report.normalized_lots or pending["lots"],
                    execution_stop_units(spec, pending["stop_units"]),
                    execution_stop_units(spec, pending["tp_units"]),
                )
                st.session_state["_last_order_result"] = {
                    "ok": result.ok,
                    "code": result.code,
                    "message": result.message,
                    "ticket": result.ticket,
                }
            finally:
                st.session_state["submit_in_flight"] = False
                st.session_state["_pending_order"] = None
            st.rerun()


def render_order_form(whitelist: dict[str, list[str]]) -> None:
    asset = st.session_state.get("selected_asset")
    category = st.session_state.get("asset_category", "forex")
    from ui.data_access import get_account_snapshot, get_symbol_spec

    account = st.session_state.get("account") or get_account_snapshot()
    spec = get_symbol_spec(asset, category) if asset else None
    quote = {}
    currency = account.currency or "—"

    lot_default, sl_default, default_note = _apply_order_defaults(spec, category)
    with accent_card("green", "execution"):
        st.subheader("Execution")
        st.caption(default_note)
        st.segmented_control(
            "Side",
            ["BUY", "SELL"],
            key="w_order_side",
            on_change=persist_order_side,
            label_visibility="collapsed",
        )

        order_type_cols = st.columns([1.4, 1, 1], gap="small")
        with order_type_cols[0]:
            st.selectbox(
                "Order type",
                ["Market"],
                key="w_order_type",
                on_change=persist_order_type,
                help=MARKET_HELP,
            )
        with order_type_cols[1]:
            st.button("Limit", disabled=True, help=LIMIT_HELP, key="btn_limit_disabled", width="stretch")
            st.caption(UNSUPPORTED)
        with order_type_cols[2]:
            st.button("Conditional", disabled=True, help=CONDITIONAL_HELP, key="btn_conditional_disabled", width="stretch")
            st.caption(UNSUPPORTED)

        st.slider(
            "Free-margin allocation",
            min_value=0,
            max_value=100,
            key="w_exposure_pct",
            on_change=persist_exposure_pct,
            format="%d%%",
        )
        risk_col, slip_col = st.columns(2, gap="small")
        with risk_col:
            st.number_input(
                "Lot size",
                min_value=0.0,
                max_value=max(100.0, float(spec.volume_max)) if spec else 100.0,
                step=float(spec.volume_step) if spec and spec.volume_step > 0 else 0.01,
                format="%.2f",
                key="w_lot_size",
                on_change=persist_lot_size,
            )
        with slip_col:
            st.number_input(
                "Slippage (pips)", min_value=0.0, step=0.1, format="%.1f",
                key="w_slippage_pips", on_change=persist_slippage_pips,
            )

        stop_label = "Pips" if category == "forex" else "Points"
        st.number_input(
            f"Stop loss ({stop_label.lower()})",
            min_value=0.0,
            step=1.0,
            key="w_sl_pips",
            on_change=persist_sl_pips,
        )

        lots = _effective_lots(spec, account)
        stop_units = float(st.session_state.get("sl_pips", 0.0) or 0.0)
        direction = st.session_state.get("order_side", "BUY")
        report, context = inspect_order(
            asset=asset or "",
            category=category,
            direction=direction,
            lots=lots,
            stop_units=stop_units,
            slippage=float(st.session_state.get("slippage_pips", 0.0) or 0.0),
            whitelist=whitelist,
        )
        spec = context.get("spec")
        required = context.get("required")
        risk_amount, risk_pct = (math.inf, None)
        if spec and lots > 0 and stop_units > 0:
            risk_amount, risk_pct = trade_risk(spec, lots, stop_units, account.equity)
        disabled = not report.ok or bool(st.session_state.get("submit_in_flight", False))
        if disabled and st.session_state.get("submit_in_flight", False):
            st.caption("Submit is disabled while a previous submit is still in flight.")

        shared = st.session_state.get("_run_snapshot")
        calendar = shared.news.events if shared is not None else fetch_news().events
        news_event = high_impact_within(
            calendar,
            currencies_for_symbol(asset),
            minutes=30,
        )
        if news_event:
            st.warning(f"High-impact news risk · {countdown_text(news_event)}")

        spread_cost = _spread_cost(context.get("quote", {}), spec, lots)
        st.caption(
            f"Spread cost: {format_money(spread_cost, currency) if spread_cost is not None else '—'}"
            "  ·  Commission: —"
        )

        submit_label = f"{direction} · {asset or '—'} · {lots:.2f} lots"
        if st.button(submit_label, key="btn_submit_order", type="primary", width="stretch", disabled=disabled):
            token = uuid.uuid4().hex
            st.session_state["_pending_order"] = {
                "asset": asset,
                "category": category,
                "direction": direction,
                "lots": lots,
                "stop_units": stop_units,
                "tp_units": stop_units * 2,
                "stop_label": stop_label,
                "slippage": float(st.session_state.get("slippage_pips", 0.0) or 0.0),
                "whitelist": whitelist,
                "token": token,
                "risk_display": format_money(risk_amount, currency) if math.isfinite(risk_amount) else "—",
                "margin_display": format_money(required, currency) if required is not None else "—",
            }
        if st.session_state.get("_pending_order"):
            _confirm_order_dialog()

        result = st.session_state.get("_last_order_result")
        if result:
            if result["ok"]:
                st.success(result["message"])
            else:
                st.error(f"{result['code']}: {result['message']}")

    risk_accent = risk_card_accent([failure.code for failure in report.failures])
    with accent_card(risk_accent, "risk"):
        st.subheader("Risk")
        _render_risk_summary(risk_amount, risk_pct, required, account, currency, risk_accent)
        if report.failures:
            for failure in report.failures:
                st.caption(f"{failure.code}: {failure.message}")
        elif report.ok:
            st.caption("Checks passed")


def _apply_order_defaults(spec, category: str) -> tuple[float, float, str]:
    """Restore 0.01 lots and 30 pips unless the broker spec forbids them."""
    from ui.risk_calc import pip_size

    lot = 0.01
    lot_note = "Lot default: Instructions_1 value 0.01"
    if spec is not None and spec.volume_step > 0 and spec.volume_min > 0:
        step = float(spec.volume_step)
        minimum = float(spec.volume_min)
        steps_from_min = (lot - minimum) / step
        aligned = abs(steps_from_min - round(steps_from_min)) < 1e-6
        if lot + 1e-9 < minimum or not aligned:
            lot = minimum
            lot_note = (
                "Lot default: symbol spec forbids 0.01 "
                f"(volume_min={minimum:g}, volume_step={step:g}); using {lot:g}"
            )
    sl = 30.0
    unit = "pips" if category == "forex" else "points"
    sl_note = f"SL default: Instructions_1 value 30 {unit}"
    if spec is not None and spec.stops_level_points > 0 and spec.point > 0:
        minimum_units = spec.stops_level_points * float(spec.point) / pip_size(spec)
        if sl + 1e-9 < minimum_units:
            sl = float(math.ceil(minimum_units))
            sl_note = f"SL default: symbol spec stops_level forbids 30; using {sl:g} {unit}"

    current_lot = float(st.session_state.get("lot_size") or 0)
    if current_lot <= 0 or (spec is not None and current_lot + 1e-9 < float(spec.volume_min or 0)):
        st.session_state["lot_size"] = lot
        st.session_state["w_lot_size"] = lot
    else:
        lot_note = f"Lot size: saved value {current_lot:.2f}"
        if widget_follows_mirror(st.session_state.get("w_lot_size"), current_lot):
            st.session_state["w_lot_size"] = current_lot
    spec_floor = 0.0
    if spec is not None and spec.stops_level_points > 0 and spec.point > 0:
        spec_floor = spec.stops_level_points * float(spec.point) / pip_size(spec)
    current_sl = float(st.session_state.get("sl_pips") or 0)
    # A saved stop above zero stays unless the broker minimum is tighter than it.
    if current_sl <= 0 or current_sl + 1e-9 < spec_floor:
        st.session_state["sl_pips"] = sl
        st.session_state["w_sl_pips"] = sl
    else:
        sl_note = f"SL: saved value {current_sl:g} {unit}"
        if widget_follows_mirror(st.session_state.get("w_sl_pips"), current_sl):
            st.session_state["w_sl_pips"] = current_sl
    return lot, sl, f"{lot_note}. {sl_note}."


def _effective_lots(spec, account) -> float:
    raw = float(st.session_state.get("lot_size", 0.01) or 0.0)
    exposure_pct = int(st.session_state.get("exposure_pct", 0) or 0)
    if exposure_pct <= 0:
        return raw
    if not spec or not account.free_margin or account.free_margin <= 0:
        st.caption("Exposure sizing unavailable · enter lot size")
        return 0.0
    from ui.data_access import get_required_margin

    margin_per_lot = get_required_margin(spec.symbol, st.session_state.get("order_side", "BUY"), 1.0)
    if margin_per_lot is None or margin_per_lot <= 0:
        st.caption("Exposure sizing unavailable · enter lot size")
        return 0.0
    raw = (exposure_pct / 100 * account.free_margin) / margin_per_lot
    try:
        normalized = normalize_lots(raw, spec)
    except ValueError:
        return 0.0
    st.caption(f"Exposure estimate · {normalized:.2f} lots")
    return normalized


def _render_risk_summary(risk_amount, risk_pct, required, account, currency: str, accent: str) -> None:
    risk_label = format_money(risk_amount, currency) if math.isfinite(risk_amount) else "N/A"
    pct_label = f"{risk_pct:.2f}%" if risk_pct is not None and math.isfinite(risk_pct) else "N/A"
    sentence = f"Open risk · {risk_label}  ·  {pct_label} of equity"
    level = risk_summary_level(accent, risk_pct)
    if level == "info":
        st.info(f"Open risk · {risk_label}  ·  {pct_label}")
    elif level == "error":
        st.error(sentence)
    elif level == "warning":
        st.warning(sentence)
    else:
        st.success(sentence)
    projected_free = (
        account.free_margin - required
        if account.free_margin is not None and required is not None
        else None
    )
    projected_level = (
        account.equity / (account.margin_used + required) * 100
        if account.equity and account.margin_used is not None and required is not None
        and account.margin_used + required > 0
        else None
    )
    margin_text = format_money(projected_free, currency) if projected_free is not None else "—"
    level_text = f"{projected_level:.1f}%" if projected_level is not None else "N/A"
    st.caption(f"Projected free margin: {margin_text}  ·  Projected level: {level_text}")


def _spread_cost(quote: dict, spec, lots: float) -> float | None:
    if not spec or not quote.get("valid") or not lots or spec.tick_size <= 0:
        return None
    try:
        return float(quote["spread"]) * spec.point / spec.tick_size * spec.tick_value_loss * lots
    except (KeyError, TypeError, ValueError):
        return None