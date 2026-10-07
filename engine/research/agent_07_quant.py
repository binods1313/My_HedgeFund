"""07 QuantAgent - statistical edge: z-score, rolling win-prob, factor tilt"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


AGENT_ID = "07"
AGENT_NAME = "Quant Agent"
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
    """Calculate quantitative edge metrics: z-score, rolling win probability, factor tilt.

    Returns:
        - score: -1 to 1 quant edge (-1 = bearish edge, 0 = no edge, 1 = bullish edge)
        - label: "EDGE_BULLISH" | "EDGE_NEUTRAL" | "EDGE_BEARISH"
        - confidence: based on sample size and z-score magnitude
    """
    try:
        returns = context.get("returns", np.array([]))
        if len(returns) < 30:
            return AgentResult(
                agent_id=AGENT_ID,
                name=AGENT_NAME,
                layer=LAYER,
                score=None,
                label="NEUTRAL",
                confidence=0.0,
                features={},
                ok=False,
                error="insufficient data",
                latency_ms=context.get("latency_ms", 0.0),
            )

        # Calculate rolling z-score of returns
        mean = np.mean(returns)
        std = np.std(returns)
        if std == 0:
            z_score = 0
        else:
            z_score = (np.mean(returns[-20:]) - mean) / std

        # Rolling win probability (positive returns)
        win_prob = np.mean(returns > 0) if len(returns) > 0 else 0.5

        # Factor tilt: measure exposure to common factors
        # Simple momentum factor: recent returns momentum
        momentum = np.mean(returns[-10:]) * 10  # annualized approx

        # Overall edge score: combine z-score and win probability
        edge_score = (z_score * 0.5 + (win_prob - 0.5) * 0.5) * 100  # Scale to -50 to 50

        # Clamp to -100 to 100
        edge_score = max(-100, min(100, edge_score))

        # Determine label
        if edge_score > 30:
            label = "EDGE_BULLISH"
        elif edge_score < -30:
            label = "EDGE_BEARISH"
        else:
            label = "EDGE_NEUTRAL"

        # Confidence based on sample size and z-score magnitude
        sample_size = len(returns)
        z_magnitude = abs(z_score)
        confidence = min(1.0, sample_size / 100 * (1 + abs(z_magnitude) * 0.1))

        features = {
            "sample_size": sample_size,
            "z_score": round(z_score, 3),
            "win_probability": round(win_prob * 100, 1),
            "momentum": round(momentum, 4),
            "mean_return": float(np.mean(returns)),
            "std_return": float(std),
        }

        return AgentResult(
            agent_id=AGENT_ID,
            name=AGENT_NAME,
            layer=LAYER,
            score=round(edge_score, 1),
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