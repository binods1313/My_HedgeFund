"""Research Desk. Shadow outputs stay on this page and never reach an order."""

from __future__ import annotations

import inspect
import json
import sqlite3
from datetime import datetime, timezone

import plotly.graph_objects as go
import streamlit as st

from config_ext_loader import load_config_ext
from engine.research import AGENT_IDS, AGENT_MODULES
from learning.calibrate import by_bucket, had_edge, summarise
from learning.ledger import connect, latest_predictions, list_proposals, samples_for, set_proposal_status
from learning.propose import apply_proposal, current_values, list_versions, rollback_to
from learning.runtime import ensure_thread, sync_flag
from learning.shadow import ROOT
from ui.components.cards import accent_card
from ui.components.infoway_panel import render_infoway_chip
from ui.services.dexter_client import DEXTER_ROOT, budget_today, latest_cached, name_is_set
from ui.theme import plotly_theme

_LEDGER = ROOT / "data" / "learning.db"
_WEIGHTS = ROOT / "data" / "learning_weights"
_BANNER = "Shadow mode: these outputs do not affect trading."
_NAMES = {
    "01": "Market regime",
    "02": "Technical",
    "03": "Multi-timeframe",
    "04": "News",
    "05": "Sentiment",
    "06": "Fundamental",
    "07": "Quant",
    "08": "Strategy",
    "09": "Risk adapter",
    "10": "Execution adapter",
}
_DETERMINISTIC = {"09", "10"}

#: §A5 implementation-status vocabulary for the analysis cards. The engine
#: loop wires no research module today, so "Implemented, live" never renders
#: yet; it is reserved for a module the engine actually invokes. "Not
#: implemented" is the rule for a true placeholder whose run() is a stub. The
#: old single word "OFF" wrongly implied a module did not exist, so it is gone.
_STATUS_LIVE = "Implemented, live"
_STATUS_DISABLED = "Not running yet (planned)"
_STATUS_DETERMINISTIC = "Deterministic"
_STATUS_NOT_IMPLEMENTED = "Not implemented"
_STATUS_WORDS = (
    _STATUS_LIVE,
    _STATUS_DISABLED,
    _STATUS_DETERMINISTIC,
    _STATUS_NOT_IMPLEMENTED,
)


def render_research_desk(whitelist: object) -> None:
    """Cards, curves, and proposal buttons. Nothing here submits an order."""
    with st.container(border=True):
        st.text(_BANNER)
    enabled = st.toggle("Shadow mode", key="shadow_mode")
    sync_flag(bool(enabled))
    if enabled:
        ensure_thread(True)
    render_infoway_chip()
    symbols = _symbols(whitelist)
    symbol = st.selectbox("Symbol", symbols or ["—"], key="research_symbol")
    settings = load_config_ext()
    horizon = settings.learning.horizons[0]
    conn = _open()
    try:
        rows = _latest(conn, symbol)
        _agent_cards(conn, rows, horizon, settings.learning.min_samples)
        _dexter_card(conn, horizon, settings.learning.min_samples)
        _reliability(conn, horizon, settings.learning.min_samples)
        _proposals(conn)
    finally:
        if conn is not None:
            conn.close()
    if st.session_state.get("_desk_action"):
        _proposal_dialog()


def _symbols(whitelist: object) -> list[str]:
    names: list[str] = []
    if isinstance(whitelist, dict):
        for values in whitelist.values():
            if isinstance(values, list):
                names.extend(str(item) for item in values)
    elif isinstance(whitelist, list):
        names.extend(str(item) for item in whitelist)
    return names


def _open() -> sqlite3.Connection | None:
    try:
        return connect(_LEDGER)
    except (OSError, RuntimeError, sqlite3.Error):
        return None


def _latest(conn: sqlite3.Connection | None, symbol: str) -> dict[str, sqlite3.Row]:
    if conn is None or symbol == "—":
        return {}
    return {str(row["agent_id"]): row for row in latest_predictions(conn, symbol)}


def _agent_cards(conn, rows: dict, horizon: int, minimum: int) -> None:
    st.subheader("Analysis")
    columns = st.columns(2)
    for index, agent_id in enumerate(AGENT_IDS):
        with columns[index % 2]:
            with accent_card("research", f"agent-{agent_id}"):
                st.text(f"{agent_id} {_NAMES.get(agent_id, 'Analysis')}")
                st.text(_implementation_status(agent_id))
                st.caption(_disabled_reason(agent_id))
                if agent_id in _DETERMINISTIC:
                    continue
                row = rows.get(agent_id)
                if row is None:
                    st.caption("no closed-bar record yet")
                    continue
                status = _status(row)
                st.text(f"{status} · {row['direction']} · score {_score(row['score'])}")
                st.caption(f"age {_age(str(row['ts']))}")
                st.caption(_track(conn, agent_id, horizon, minimum))


#: Agent 06 reads fundamentals through the Dexter adapter, which needs this key.
_AGENT_KEYS: dict[str, str] = {"06": "FINANCIAL_DATASETS_API_KEY"}


def _disabled_reason(agent_id: str) -> str:
    """Why a card is not live, stated from code and config -- never a guess.

    Agents 01-08 make no model call and need no provider key; the missing piece
    is an engine caller for the research modules. Agent 06 additionally needs
    its data key, which is reported as not set when it is absent from ``.env``.
    """
    if agent_id in _DETERMINISTIC:
        if agent_id == "09":
            return "Reads the latest risk decision from the journal"
        return "Reads the latest execution decision from the journal"
    module = AGENT_MODULES.get(agent_id)
    if module is None or _is_stub(module):
        return "Not built yet"
    key = _AGENT_KEYS.get(agent_id)
    if key and not name_is_set(DEXTER_ROOT / ".env", key):
        return f"Needs {key} (not set)"
    return "Planned: runs after the signal step (Stage 4)"


def _implementation_status(agent_id: str) -> str:
    """The §A5 word for a module, from its code -- never the empty ledger.

    The engine loop calls no research module today, so nothing is live yet.
    A module whose ``run`` is a true placeholder reads "Not implemented";
    the two deterministic adapters read "Deterministic"; every other real
    module reads "Not running yet (planned)" because it has no live caller.
    """
    module = AGENT_MODULES.get(agent_id)
    if module is None or _is_stub(module):
        return _STATUS_NOT_IMPLEMENTED
    if agent_id in _DETERMINISTIC:
        return _STATUS_DETERMINISTIC
    return _STATUS_DISABLED


def _is_stub(module: object) -> bool:
    """True when a module's ``run`` only raises, i.e. it is a placeholder."""
    run = getattr(module, "run", None)
    if run is None:
        return True
    try:
        source = inspect.getsource(run)
    except (OSError, TypeError):
        return False
    return "NotImplementedError" in source


def _status(row: sqlite3.Row) -> str:
    try:
        payload = json.loads(row["features_json"])
    except (TypeError, json.JSONDecodeError):
        return "DEGRADED"
    if not isinstance(payload, dict):
        return "DEGRADED"
    if payload.get("status") == "na":
        return "N-A"
    if payload.get("ok") is True:
        return "OK"
    label = str(payload.get("label") or "").upper()
    error = str(payload.get("error") or "").lower()
    if "N/A" in label or "NO " in error or "unavailable" in error:
        return "N-A"
    return "DEGRADED"


def _score(value: object) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.2f}"
    return "—"


def _age(stamp: str) -> str:
    try:
        when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return "unknown"
    seconds = max(0, int((datetime.now(timezone.utc) - when).total_seconds()))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def _track(conn, agent_id: str, horizon: int, minimum: int) -> str:
    if conn is None:
        return "samples 0 · INSUFFICIENT"
    summary = summarise(samples_for(conn, agent_id, horizon), minimum=minimum)
    if summary["insufficient"]:
        return f"samples {summary['count']} · INSUFFICIENT"
    rate = _pct(summary["hit_rate"])
    low = _pct(summary["low"])
    high = _pct(summary["high"])
    mean_r = "—" if summary["mean_r"] is None else f"{summary['mean_r']:.2f}"
    return f"samples {summary['count']} · hit {rate} ({low}–{high}) · mean R {mean_r}"


def _pct(value: object) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value) * 100:.1f}%"
    return "—"


def _dexter_card(conn, horizon: int, minimum: int) -> None:
    report = latest_cached()
    count, cap = budget_today()
    calls = "unreadable" if count is None else f"{count} / {cap}"
    live_key = name_is_set(DEXTER_ROOT / ".env", "FINANCIAL_DATASETS_API_KEY")
    with accent_card("research", "dexter"):
        st.text("Shadow research")
        if report is None:
            st.text("No shadow read yet.")
        else:
            st.text(f"{report.symbol} · {report.status}")
            st.text(report.summary)
            st.caption("sources: " + (", ".join(report.sources) or "none"))
            st.caption(f"data age {_age(report.as_of)}")
        st.caption(f"calls today {calls}")
        if report is None or report.model == "fixture" or not live_key:
            st.caption("fixture")
        st.caption("source: shadow process")
        if conn is not None:
            samples = samples_for(conn, "dexter", horizon)
            edge = had_edge(samples, minimum=minimum)
            label = {True: "yes", False: "no"}.get(edge, "unknown")
            st.caption(f"edge: {label}")


def _reliability(conn, horizon: int, minimum: int) -> None:
    st.subheader("Reliability")
    agent_id = st.selectbox(
    "Analysis",
        [item for item in AGENT_IDS if item not in _DETERMINISTIC] + ["shadow"],
        key="research_reliability_agent",
    )
    if agent_id == "shadow":
        agent_id = "dexter"
    if conn is None:
        st.caption("No resolved samples yet.")
        return
    buckets = by_bucket(samples_for(conn, agent_id, horizon), minimum=minimum)
    if not buckets:
        st.caption("No resolved samples yet.")
        return
    figure = go.Figure(
        go.Bar(
            x=[row["bucket"] for row in buckets],
            y=[0 if row["hit_rate"] is None else row["hit_rate"] for row in buckets],
            text=[str(row["count"]) for row in buckets],
            textposition="outside",
        )
    )
    figure.update_layout(**plotly_theme(), yaxis_title="Hit rate", xaxis_title="Score bucket")
    st.plotly_chart(figure, width="stretch", config={"displayModeBar": False}, key="research_reliability")
    thin = [row["bucket"] for row in buckets if row["insufficient"]]
    if thin:
        st.caption("INSUFFICIENT: " + ", ".join(thin))


def _proposals(conn) -> None:
    st.subheader("Proposals")
    st.caption("Nothing here is applied until you confirm it.")
    if conn is None:
        st.caption("No proposals yet.")
        return
    rows = list_proposals(conn)
    if not rows:
        st.caption("No proposals yet.")
        return
    for row in rows:
        proposal_id = int(row["id"])
        with accent_card("research", f"proposal-{proposal_id}"):
            st.text(f"#{proposal_id} {row['kind']} · {row['status']}")
            st.text(f"{row['old_value']} → {row['new_value']}")
            evidence = _evidence(str(row["evidence_json"]))
            for line in evidence:
                st.text(line)
            _proposal_buttons(row)


def _evidence(raw: str) -> list[str]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ["evidence unreadable"]
    if not isinstance(payload, dict):
        return ["evidence unreadable"]
    lines = []
    for key in ("samples", "minimum", "max_change", "shrinkage", "older_mean_r", "newer_mean_r", "reason"):
        if key in payload:
            lines.append(f"{key}: {payload[key]}")
    return lines or ["no evidence fields"]


def _proposal_buttons(row: sqlite3.Row) -> None:
    status = str(row["status"])
    proposal_id = int(row["id"])
    actions = []
    if status == "proposed":
        actions.append(("Approve", "approve"))
    if status in {"proposed", "approved"}:
        actions.extend([("Reject", "reject"), ("Apply", "apply")])
    if status == "applied":
        actions.append(("Rollback", "rollback"))
    if not actions:
        return
    columns = st.columns(len(actions))
    for column, (label, operation) in zip(columns, actions):
        with column:
            if st.button(label, key=f"desk_{operation}_{proposal_id}", width="stretch"):
                st.session_state["_desk_action"] = {
                    "op": operation,
                    "id": proposal_id,
                    "kind": str(row["kind"]),
                    "old_value": str(row["old_value"]),
                    "new_value": str(row["new_value"]),
                    "status": status,
                    "evidence_json": str(row["evidence_json"]),
                }


@st.dialog("Confirm proposal", width="small")
def _proposal_dialog() -> None:
    action = st.session_state.get("_desk_action")
    if not isinstance(action, dict):
        return
    st.text(f"{action['op']} #{action['id']} {action['kind']}")
    st.text(f"{action['old_value']} → {action['new_value']}")
    for line in _evidence(str(action.get("evidence_json") or "")):
        st.text(line)
    version = None
    if action["op"] == "rollback":
        versions = list_versions(_WEIGHTS)
        if not versions:
            st.caption("No weights version to restore.")
            return
        version = st.selectbox("Restore version", versions, key="desk_rollback_version")
    cancel, confirm = st.columns(2)
    with cancel:
        if st.button("Cancel", key="desk_proposal_cancel", width="stretch"):
            st.session_state["_desk_action"] = None
            st.rerun()
    with confirm:
        if st.button("Confirm", key="desk_proposal_confirm", type="primary", width="stretch"):
            _apply_action(action, version)
            st.session_state["_desk_action"] = None
            st.rerun()


def _apply_action(action: dict, version: int | None) -> None:
    conn = _open()
    if conn is None:
        return
    try:
        proposal_id = int(action["id"])
        operation = action["op"]
        if operation == "approve" and action["status"] == "proposed":
            set_proposal_status(conn, proposal_id, "approved")
        elif operation == "reject" and action["status"] in {"proposed", "approved"}:
            set_proposal_status(conn, proposal_id, "rejected")
        elif operation == "apply" and action["status"] in {"proposed", "approved"}:
            values = current_values(_WEIGHTS)
            values[str(action["kind"])] = str(action["new_value"])
            apply_proposal(conn, proposal_id, _WEIGHTS, values)
        elif operation == "rollback" and action["status"] == "applied" and version is not None:
            rollback_to(conn, proposal_id, _WEIGHTS, int(version))
    except (OSError, ValueError, sqlite3.Error):
        return
    finally:
        conn.close()
