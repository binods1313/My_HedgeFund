"""04 NewsAgent - macro calendar events (ForexFactory)"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from datetime import datetime, timezone


AGENT_ID = "04"
AGENT_NAME = "News Agent"
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
    """Analyze upcoming macro calendar events from ForexFactory.

    Returns:
        - score: impact weight (0-100)
        - label: "HIGH_IMPACT" | "MEDIUM_IMPACT" | "LOW_IMPACT" | "NONE"
        - confidence: based on event recency and data quality
    """
    try:
        events = context.get("events", [])
        if not events:
            return AgentResult(
                agent_id=AGENT_ID,
                name=AGENT_NAME,
                layer=LAYER,
                score=None,
                label="NONE",
                confidence=0.0,
                features={},
                ok=False,
                error="no event data",
                latency_ms=context.get("latency_ms", 0.0),
            )

        now = datetime.now(timezone.utc)
        high_impact = 0
        medium_impact = 0
        low_impact = 0
        processed = 0

        for event in events:
            try:
                processed += 1
                # Extract event time and impact
                impact = event.get("impact", "").lower()
                event_time_str = event.get("time", "")
                event_date = event.get("date", "")

                # Parse time (simplified)
                if impact == "high":
                    high_impact += 1
                elif impact == "medium":
                    medium_impact += 1
                else:
                    low_impact += 1
            except Exception:
                continue

        total = high_impact + medium_impact + low_impact

        if high_impact > 0 and (high_impact + medium_impact) > 0:
            label = "HIGH_IMPACT"
            score = min(100, 50 + high_impact * 30 + medium_impact * 10)
            confidence = min(1.0, processed / 5)
        elif medium_impact > 0:
            label = "MEDIUM_IMPACT"
            score = 30 + medium_impact * 10
            confidence = min(1.0, processed / 5)
        elif low_impact > 0:
            label = "LOW_IMPACT"
            score = low_impact * 5
            confidence = min(1.0, processed / 5)
        else:
            label = "NONE"
            score = 0
            confidence = 0.0

        features = {
            "events_processed": processed,
            "high_impact": high_impact,
            "medium_impact": medium_impact,
            "low_impact": low_impact,
            "event_details": events[:3] if events else [],
        }

        return AgentResult(
            agent_id=AGENT_ID,
            name=AGENT_NAME,
            layer=LAYER,
            score=round(score, 1),
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