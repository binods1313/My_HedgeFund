from __future__ import annotations

from datetime import datetime
import uuid

import streamlit as st

from data_engine import TIMEFRAME_H4, TIMEFRAME_M15, get_technical_dataframe
from engine.dryrun import run_dry_run
from engine.sizing import LotSizer
from metrics import format_llm_confidence
from ui.components.trading import execution_stop_units, inspect_order
from ui.data_access import fetch_live_broker_symbols, get_symbol_spec, submit_order

#: Labels the tab shows for the xAI structural gate. Constants so the string the
#: operator reads is the string the tests can pin.
GATE_VERIFIED = "VERIFIED"
GATE_REJECTED = "REJECTED"
GATE_UNAVAILABLE = "UNAVAILABLE"
GATE_NOT_CONSULTED = "NOT CONSULTED"


def _gate_label(result) -> str:
    """Map a DryRunResult onto the label the tab displays.

    `GateDecision.consulted` is False for two different situations -- the
    confluence count never reached the threshold, and the provider is not
    configured at all -- so the gate reason is what separates them. Matching on
    it is inelegant, but the alternative is a fourth field on `GateDecision`,
    and the engine layer should not grow a field to serve a UI label.

    A provider outage must read differently from a rejection: one is the model
    declining the setup, the other is the system not working. Reporting both as
    "REJECTED" would tell the operator the structure was examined when it was
    never seen.
    """
    if result.gate_passed:
        return GATE_VERIFIED
    if "Unavailable" in result.gate_reason:
        return GATE_UNAVAILABLE
    if not result.gate_consulted:
        return GATE_NOT_CONSULTED
    return GATE_REJECTED


def _confluence_counts(result) -> tuple[int, int]:
    """(agreed, required) read off the signal the dry run assembled.

    The denominator is taken from the signal rather than hard-coded to 5: the
    sweep-alignment block is only appended when the sweep disagrees with the
    requested direction, so the required count is 5 or 6 depending on the bar.
    """
    signal = result.signal
    if signal is None:
        return 0, 0
    required = signal.required_blocks
    return sum(1 for block in required if block.agrees), len(required)


def _run_strategy_check(asset: str, bars, direction: str, category: str,
                        stop_units: float):
    """Route the scan through the dry-run harness. Returns None if it cannot run.

    `get_ai_decision` answers *which way*; `run_dry_run` answers *whether the
    structure supports it*. The second question is the one that decides whether
    an order is allowed to leave, so the scan cannot stop at the first.

    The account fields the harness cannot see (start-of-day equity, peak,
    open-trade count) are approximated here. That makes its risk verdict weaker
    than the real one, which is why submission still goes through
    `inspect_order` -- the gate that holds the live snapshot. What this call
    adds is the confluence count and the xAI verdict, neither of which depends
    on account state.
    """
    account = st.session_state.get("account")
    equity = getattr(account, "equity", None)
    if not equity:
        return None

    spec = get_symbol_spec(asset, category)
    if spec is None:
        return None

    try:
        return run_dry_run(
            asset,
            bars,
            equity=float(equity),
            open_trade_count=0,
            total_open_risk=getattr(account, "open_risk", None),
            tick_value=float(spec.tick_value_loss),
            tick_size=float(spec.tick_size),
            stop_distance_pips=float(stop_units) or None,
            direction=direction,
            sizer=LotSizer(spec),
        )
    except Exception:
        # A harness that cannot run is not a harness that passed. Returning
        # None blocks the scan rather than letting it proceed unchecked.
        return None


def _format_gate_line(state: dict) -> str:
    """The one line the operator needs before deciding to execute.

    Deliberately worded to sit next to -- and not be confused with -- the
    "LLM-asserted display value" caption above it. That number is narrative:
    the model's own claim about its confidence, which gates nothing. This one
    is the structural verdict, and it is the reason an order may or may not
    leave.
    """
    total = state.get("confluences_required")
    if not total:
        return "Confluences: — | Validation Gate: NOT RUN"
    label = state.get("gate_label") or GATE_NOT_CONSULTED
    return f"Confluences: {state.get('confluences', 0)}/{total} | Validation Gate: {label}"


def render_ai_signals(whitelist: dict[str, list[str]]) -> None:
    asset = st.session_state.get("selected_asset")
    if "ai_state" not in st.session_state:
        st.session_state["ai_state"] = {
            "symbol": asset,
            "decision": "HOLD",
            "confidence": None,
            "strength": "NEUTRAL",
            "reasoning": "Standby. Run a scan to evaluate the selected instrument.",
            "risk_reward": "1:2",
            "timestamp": "—",
            "last_exec": None,
            "confluences": None,
            "confluences_required": None,
            "gate_label": None,
            "gate_reason": "",
        }
    state = st.session_state["ai_state"]
    decision = str(state.get("decision", "HOLD")).upper()
    if decision not in {"BUY", "SELL", "HOLD"}:
        decision = "HOLD"
    decision_class = {"BUY": "buy", "SELL": "sell", "HOLD": "hold"}[decision]

    with st.container(border=True, key="card-violet-signals"):
        st.subheader("AI Signal Analysis")
        st.markdown(
            f'<div class="reactor-wrap"><div class="reactor-outer {decision_class}">'
            f'<div class="reactor-inner {decision_class}">{decision}</div></div></div>',
            unsafe_allow_html=True,
        )
        confidence = format_llm_confidence(state.get("confidence"), decision)
        st.markdown(f"**AI signal · {state.get('strength', 'NEUTRAL')}**")
        st.caption(f"AI-asserted display value: {confidence}. Not an execution gate.")
        details = st.columns(3, gap="small")
        with details[0]:
            st.metric("Target", state.get("symbol") or "—")
        with details[1]:
            st.metric("Age", state.get("timestamp") or "—")
        with details[2]:
            st.metric("R : R", state.get("risk_reward") or "—")
        st.markdown(f"**{_format_gate_line(state)}**")
        st.caption("Structural gate from the dry-run harness. Unlike the "
                   "confidence figure above, this one is an execution gate.")
        st.markdown("**Portfolio manager rationale**")
        st.write(state.get("reasoning") or "—")
        st.caption("AI assertions are informational and never bypass risk validation.")

        if st.button(f"SCAN & EXECUTE · {asset or '—'}", key="btn_scan_execute", type="primary", width="stretch"):
            _run_scan(asset, whitelist)

        result = state.get("last_exec")
        if result:
            if result.get("success"):
                st.success(result.get("message", "—"))
            else:
                st.warning(result.get("message", "—"))


def _run_scan(asset: str | None, whitelist: dict[str, list[str]]) -> None:
    if not asset:
        st.error("Select a whitelisted instrument before scanning.")
        return
    if not st.session_state.get("account") or not st.session_state["account"].connected:
        st.error("Broker is disconnected. Scan is unavailable.")
        return
    try:
        with st.status(f"Scanning {asset}…", expanded=True) as status:
            status.update(label="Computing market indicators…")
            m15 = get_technical_dataframe(asset, TIMEFRAME_M15, count=100)
            h4 = get_technical_dataframe(asset, TIMEFRAME_H4, count=100)
            if m15.empty or h4.empty:
                raise RuntimeError("Fresh analysis candles are unavailable")

            status.update(label="Evaluating the setup…")
            decision = get_ai_decision(df_m15=m15, df_h4=h4, symbol=asset)
            direction = str(decision.get("decision", "HOLD")).upper()

            confluences = None
            confluences_required = None
            gate_label = None
            gate_reason = ""

            execution_result = {
                "success": True,
                "status": "HOLD",
                "message": "HOLD decision · no order submitted",
                "ticket": None,
            }
            if direction in {"BUY", "SELL"}:
                category = st.session_state.get("asset_category", "forex")
                lots = float(st.session_state.get("lot_size", 0.01) or 0.0)
                stop_units = float(st.session_state.get("sl_pips", 30.0) or 0.0)
                slippage = float(st.session_state.get("slippage_pips", 1.0) or 0.0)

                # The model decided *which way*; the harness decides whether the
                # structure supports it. Only the second can clear an order, so
                # the scan is not allowed to stop at the first.
                status.update(label="Running the dry-run strategy gate…")
                dry_run = _run_strategy_check(asset, m15, direction, category,
                                              stop_units)
                if dry_run is not None:
                    confluences, confluences_required = _confluence_counts(dry_run)
                    gate_label = _gate_label(dry_run)
                    gate_reason = dry_run.gate_reason or dry_run.reason

                if dry_run is None:
                    # The harness could not run -- no equity, no tick spec, or it
                    # raised. None of those is a pass, so the scan stops here.
                    execution_result = {
                        "success": False,
                        "status": "GATE_UNAVAILABLE",
                        "message": ("Blocked: the structural gate could not be "
                                    "evaluated, so this setup is unverified."),
                        "ticket": None,
                    }
                elif not dry_run.gate_passed:
                    execution_result = {
                        "success": False,
                        "status": "GATE_BLOCKED",
                        "message": (f"Blocked by the structural gate "
                                    f"({confluences}/{confluences_required} "
                                    f"confluences · validation {gate_label}): "
                                    f"{gate_reason}"),
                        "ticket": None,
                    }
                else:
                    # Session-scoped idempotency token (not a credential;
                    # prevents duplicate submissions only).
                    session_run_counter = "session_run_counter"
                    report, context = inspect_order(
                        asset=asset,
                        category=category,
                        direction=direction,
                        lots=lots,
                        stop_units=stop_units,
                        slippage=slippage,
                        whitelist=whitelist,
                        order_token=session_run_counter,
                    )
                    if not report.ok:
                        execution_result = {
                            "success": False,
                            "status": "RISK_REJECTED",
                            "message": "; ".join(f"{f.code}: {f.message}" for f in report.failures),
                            "ticket": None,
                        }
                    else:
                        spec = context["spec"]
                        result = submit_order(
                            asset,
                            direction,
                            report.normalized_lots or lots,
                            execution_stop_units(spec, stop_units),
                            execution_stop_units(spec, stop_units * 2),
                            # The anchor this intent was actually built on. The
                            # guard must re-check this structure, not derive a
                            # different one from a fresh snapshot.
                            leg=getattr(dry_run.signal, "leg", None),
                        )
                        st.session_state["last_execution_token"] = token
                        execution_result = {
                            "success": result.ok,
                            "status": result.code,
                            "message": result.message,
                            "ticket": result.ticket,
                        }

            st.session_state["ai_state"] = {
                "symbol": asset,
                "decision": direction,
                "confidence": decision.get("confidence"),
                "strength": decision.get("strength", "NEUTRAL"),
                "reasoning": decision.get("reasoning", "—"),
                "risk_reward": decision.get("risk_reward", "1:2"),
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "last_exec": execution_result,
                "confluences": confluences,
                "confluences_required": confluences_required,
                "gate_label": gate_label,
                "gate_reason": gate_reason,
            }
            status.update(label="Scan complete", state="complete", expanded=False)
        st.rerun()
    except Exception as exc:
        st.session_state["ai_state"]["last_exec"] = {
            "success": False,
            "status": "SCAN_ERROR",
            "message": f"Scan unavailable: {exc}",
            "ticket": None,
        }
        st.rerun()