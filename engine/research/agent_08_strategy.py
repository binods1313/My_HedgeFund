"""08 StrategyAgent - converts upstream outputs into deterministic rules"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


AGENT_ID = "08"
AGENT_NAME = "Strategy Agent"
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
    """Convert upstream agent outputs into deterministic trading rules.

    Returns:
        - score: count of triggered rules (0-5)
        - label: "RULES_TRIGGERED" | "NO_RULES" | "CONFLICTING_RULES"
        - confidence: based on rule consistency
    """
    try:
        confluence_data = context.get("confluence", {})
        if confluence_data is None:
            return AgentResult(
                agent_id=AGENT_ID,
                name=AGENT_NAME,
                layer=LAYER,
                score=None,
                label="NO_RULES",
                confidence=0.0,
                features={},
                ok=False,
                error="no confluence data",
                latency_ms=context.get("latency_ms", 0.0),
            )

        # Extract rule triggers from confluence data
        # Each agent contributes a rule status
        rule_statuses = confluence.get("rule_statuses", {})
        
        if not rule_statuses:
            return AgentResult(
                agent_id=AGENT_ID,
                name=AGENT_NAME,
                layer=LAYER,
                score=None,
                label="NO_RULES",
                confidence=0.0,
                features={},
                ok=False,
                error="no rule statuses",
                latency_ms=context.get("latency_ms", 0.0),
            )

        # Count triggered rules
        triggered = 0
        total = 0
        conflict_count = 0

        for rule_name, status in rule_statuses.items():
            total += 1
            if status.get("triggered", False):
                triggered += 1
            if status.get("conflicting", False):
                conflict_count += 1

        # Determine result
        if triggered == 0:
            label = "NO_RULES"
            score = 0
            confidence = 0.0
        elif conflict_count > 0:
            label = "CONFLICTING_RULES"
            score = triggered
            confidence = max(0.0, 1.0 - conflict_count * 0.2)
        else:
            label = "RULES_TRIGGERED"
            score = triggered
            confidence = min(1.0, triggered / 5.0)

        features = {
            "total_rules": total,
            "triggered_rules": triggered,
            "conflict_count": conflict_count,
            "rule_statuses": rule_statuses,
        }

        return AgentResult(
            agent_id=AGENT_ID,
            name=AGENT_NAME,
            layer=LAYER,
            score=score,
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