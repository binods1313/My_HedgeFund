"""06 FundamentalAgent - via Dexter adapter behind interface"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


AGENT_ID = "06"
AGENT_NAME = "Fundamental Agent"
LAYER = "RESEARCH"


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
    """Analyze fundamental data via Dexter adapter.

    Returns:
        - score: 0-100 fundamental strength score
        - label: "STRONG" | "FAIR" | "WEAK" | "N/A"
        - confidence: based on data availability and recency
    """
    try:
        fundamental_data = context.get("fundamental_data", {})
        if not fundamental_data:
            return AgentResult(
                agent_id=AGENT_ID,
                name=AGENT_NAME,
                layer=LAYER,
                score=None,
                label="N/A",
                confidence=0.0,
                features={},
                ok=False,
                error="no fundamental data",
                latency_ms=context.get("latency_ms", 0.0),
            )

        # Extract key fundamental metrics
        # This is a simplified adapter - in production would call Dexter API
        growth_rate = fundamental_data.get("growth_rate", 0)
        inflation_rate = fundamental_data.get("inflation_rate", 0)
        unemployment = fundamental_data.get("unemployment", 0)
        debt_to_gdp = fundamental_data.get("debt_to_gdp", 0)

        # Calculate composite fundamental score
        # Healthy growth > 2%, inflation < 3%, unemployment < 6%, debt < 60% of GDP
        score_components = {
            "growth": max(0, min(25, 25 + growth_rate * 5)),  # 25-50 points
            "inflation": max(0, min(25, 25 - inflation_rate * 3)),  # 25-0 points
            "unemployment": max(0, min(25, 25 - unemployment * 2)),  # 25-0 points
            "debt": max(0, min(25, 25 - debt_to_gdp * 0.4)),  # 25-0 points
        }

        total_score = sum(score_components.values())

        # Determine label
        if total_score >= 75:
            label = "STRONG"
        elif total_score >= 50:
            label = "FAIR"
        elif total_score > 0:
            label = "WEAK"
        else:
            label = "N/A"

        confidence = min(1.0, 0.5 + 0.05 * sum(1 for v in score_components.values() if v > 10))

        features = {
            "growth_rate": growth_rate,
            "inflation_rate": inflation_rate,
            "unemployment": unemployment,
            "debt_to_gdp": debt_to_gdp,
            "score_components": score_components,
            "detail": f"Growth: {growth_rate:.1f}%, Inflation: {inflation_rate:.1f}%, Unemployment: {unemployment:.1f}%, Debt/DP: {debt_to_gdp:.1f}%",
        }

        return AgentResult(
            agent_id=AGENT_ID,
            name=AGENT_NAME,
            layer=LAYER,
            score=round(total_score, 1),
            label=label,
            confidence=round(confidence, 2),
            features=features,
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