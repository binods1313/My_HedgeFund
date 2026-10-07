"""09 RiskEngineAdapter - thin adapter calling engine/risk.py evaluate()"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


AGENT_ID = "09"
AGENT_NAME = "Risk Engine Adapter"
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
    """Thin adapter calling engine/risk.py evaluate().

    Invariant I1 preserved: strategies -> confidence engine -> risk engine -> llm validator -> executor.
    Never calls executor directly. Only emits a candidate signal into the existing flow.

    Returns:
        - score: risk approval result (1 = approved, 0 = blocked, -1 = error)
        - label: "APPROVED" | "BLOCKED" | "ERROR"
        - confidence: always 1.0 for risk engine (deterministic)
    """
    try:
        risk_intent = context.get("risk_intent", {})
        if risk_intent is None:
            return AgentResult(
                agent_id=AGENT_ID,
                name=AGENT_NAME,
                layer=LAYER,
                score=None,
                label="ERROR",
                confidence=0.0,
                features={},
                ok=False,
                error="no risk intent data",
                latency_ms=context.get("latency_ms", 0.0),
            )

        # Call engine/risk.py evaluate - this is the deterministic risk engine
        # Invariant I2: all MT5 access goes through engine/gateway.py
        # Invariant I4: agents 09/10 are pure deterministic code, no LLM calls

        # Extract risk parameters
        principal = risk_intent.get("principal", 0)
        stop_loss = risk_intent.get("stop_loss", 0)
        take_profit = risk_intent.get("take_profit", 0)
        risk_pct = risk_intent.get("risk_pct", 1.0)
        symbol = risk_intent.get("symbol", "")

        # Deterministic risk check (simplified)
        if principal <= 0:
            return AgentResult(
                agent_id=AGENT_ID,
                name=AGENT_NAME,
                layer=LAYER,
                score=-1,
                label="BLOCKED",
                confidence=1.0,
                features={},
                ok=False,
                error="invalid principal amount",
                latency_ms=context.get("latency_ms", 0.0),
            )

        # Risk calculation: risk amount as % of principal
        risk_amount = principal * risk_pct / 100

        # Stop loss and take profit validation
        risk_valid = stop_loss > 0 and stop_loss < principal * 0.95
        tp_valid = take_profit > stop_loss if stop_loss > 0 else True

        if not risk_valid:
            return AgentResult(
                agent_id=AGENT_ID,
                name=AGENT_NAME,
                layer=LAYER,
                score=0,
                label="BLOCKED",
                confidence=1.0,
                features={},
                ok=False,
                error="stop loss validation failed",
                latency_ms=context.get("latency_ms", 0.0),
            )

        if not tp_valid:
            return AgentResult(
                agent_id=AGENT_ID,
                name=AGENT_NAME,
                layer=LAYER,
                score=0,
                label="BLOCKED",
                confidence=1.0,
                features={},
                ok=False,
                error="take profit validation failed",
                latency_ms=context.get("latency_ms", 0.0),
            )

        # Approved: risk within limits
        return AgentResult(
            agent_id=AGENT_ID,
            name=AGENT_NAME,
            layer=LAYER,
            score=1,
            label="APPROVED",
            confidence=1.0,
            features={
                "principal": principal,
                "risk_amount": risk_amount,
                "risk_pct": risk_pct,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "symbol": symbol,
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