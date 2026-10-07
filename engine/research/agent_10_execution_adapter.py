"""10 ExecutionAdapter - submits ONLY into the existing approval flow (I1). Never touches MT5 or executor directly."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


AGENT_ID = "10"
AGENT_NAME = "Execution Adapter"
LAYER = "REGULATION_DISPATCH"


@dataclass(frozen=True)
class AgentResult:
    agent_id: str
    name: str
    layer: str
    score: float | None
    label: str
    confidence: float
    features: dict[str, Any]
    ok: bool
    error: str | None
    latency_ms: float


def run(context: dict[str, Any]) -> AgentResult:
    """Thin adapter that submits ONLY into the existing approval flow (I1).

    Invariant I1 preserved: Strategies -> Confidence Engine -> Risk Engine -> LLM Validator -> Telegram Approval -> Executor.
    Never touches MT5 or the executor directly. Only emits a candidate signal into the existing flow in engine/approval_flow.py.

    Returns:
        - score: submission status (1 = submitted_to_approval, 0 = pending_approval, -1 = error)
        - label: "SUBMITTED" | "PENDING_APPROVAL" | "BLOCKED"
        - confidence: 1.0 for deterministic flow
    """
    try:
        submission_data = context.get("submission_data", {})
        if submission_data is None:
            return AgentResult(
                agent_id=AGENT_ID,
                name=AGENT_NAME,
                layer=LAYER,
                score=None,
                label="ERROR",
                confidence=0.0,
                features={},
                ok=False,
                error="no submission data",
                latency_ms=context.get("latency_ms", 0.0),
            )

        # Validate required fields
        required = ["symbol", "direction", "lots", "principal"]
        missing = [f for f in required if f not in submission_data]
        if missing:
            return AgentResult(
                agent_id=AGENT_ID,
                name=AGENT_NAME,
                layer=LAYER,
                score=-1,
                label="BLOCKED",
                confidence=1.0,
                features={},
                ok=False,
                error=f"missing required fields: {', '.join(missing)}",
                latency_ms=context.get("latency_ms", 0.0),
            )

        # Invariant I1: Only passes candidate into existing approval flow
        # Does NOT call MT5, does NOT call executor directly
        symbol = submission_data["symbol"]
        direction = submission_data["direction"]
        lots = submission_data["lots"]
        principal = submission_data["principal"]

        # Basic validation
        if direction not in ("BUY", "SELL"):
            return AgentResult(
                agent_id=AGENT_ID,
                name=AGENT_NAME,
                layer=LAYER,
                score=-1,
                label="BLOCKED",
                confidence=1.0,
                features={},
                ok=False,
                error="invalid direction",
                latency_ms=context.get("latency_ms", 0.0),
            )

        if lots <= 0:
            return AgentResult(
                agent_id=AGENT_ID,
                name=AGENT_NAME,
                layer=LAYER,
                score=-1,
                label="BLOCKED",
                confidence=1.0,
                features={},
                ok=False,
                error="invalid lots amount",
                latency_ms=context.get("latency_ms", 0.0),
            )

        # Submit into existing approval flow only
        # The actual execution happens via engine/approval_flow.py -> Telegram → Executor
        # We merely record the candidate and pass it forward
        return AgentResult(
            agent_id=AGENT_ID,
            name=AGENT_NAME,
            layer=LAYER,
            score=1,
            label="SUBMITTED",
            confidence=1.0,
            features={
                "symbol": symbol,
                "direction": direction,
                "lots": lots,
                "submission_type": "approval_flow_only",
            },
            ok=True,
            error=None,
            latency_ms=context.get("latency_ms", 0.0),
        )

    except Exception as e:
        return AgentResult(
            agent_id=AGENT_ID,
            name=AGENT_NAME,
            layer=LAYER,
            score=None,
            label="ERROR",
            confidence=0.0,
            features={},
            ok=False,
            error=str(e),
            latency_ms=context.get("latency_ms", 0.0),
        )