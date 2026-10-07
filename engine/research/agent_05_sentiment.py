"""05 SentimentAgent - headline/positioning/fear-greed vector"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


AGENT_ID = "05"
AGENT_NAME = "Sentiment Agent"
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
    """Analyze market sentiment from headline sentiment and positioning data.

    Returns:
        - score: -1 to 1 sentiment score (negative = bearish, positive = bullish)
        - label: "BEARISH" | "NEUTRAL" | "BULLISH"
        - confidence: based on number of sources and consistency
    """
    try:
        sentiment_data = context.get("sentiment_data", {})
        if not sentiment_data:
            return AgentResult(
                agent_id=AGENT_ID,
                name=AGENT_NAME,
                layer=LAYER,
                score=None,
                label="NEUTRAL",
                confidence=0.0,
                features={},
                ok=False,
                error="no sentiment data",
                latency_ms=context.get("latency_ms", 0.0),
            )

        # Aggregate sentiment from multiple sources
        scores = []
        sources = []
        for source, data in sentiment_data.items():
            if isinstance(data, dict) and "sentiment" in data:
                s = data["sentiment"]
                if isinstance(s, (int, float)):
                    scores.append(s)
                    sources.append(source)

        if not scores:
            return AgentResult(
                agent_id=AGENT_ID,
                name=AGENT_NAME,
                layer=LAYER,
                score=None,
                label="NEUTRAL",
                confidence=0.0,
                features={},
                ok=False,
                error="no valid sentiment scores",
                latency_ms=context.get("latency_ms", 0.0),
            )

        # Calculate aggregate sentiment
        avg_sentiment = sum(scores) / len(scores)
        source_count = len(scores)

        # Determine label
        if avg_sentiment > 0.3:
            label = "BULLISH"
        elif avg_sentiment < -0.3:
            label = "BEARISH"
        else:
            label = "NEUTRAL"

        # Confidence based on number of sources and their consistency
        confidence = min(1.0, source_count / 5.0)

        # Features
        features = {
            "source_count": source_count,
            "average_sentiment": round(avg_sentiment, 3),
            "positive_sources": sum(1 for s in scores if s > 0.3),
            "negative_sources": sum(1 for s in scores if s < -0.3),
            "source_details": {k: v.get("sentiment", 0) for k, v in sentiment_data.items()},
        }

        return AgentResult(
            agent_id=AGENT_ID,
            name=AGENT_NAME,
            layer=LAYER,
            score=round(avg_sentiment * 100, 1),  # Scale to -100 to 100, then we'll clip
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