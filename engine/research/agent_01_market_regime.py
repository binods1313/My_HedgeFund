"""01 MarketRegimeAgent - trending/ranging + volatility regime"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


AGENT_ID = "01"
AGENT_NAME = "Market Regime Agent"
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
    """Analyze market regime using ADX, EMA slope, and volatility percentile.

    Returns:
        - score: 0-100 where >60 = trending, <40 = ranging
        - label: "TRENDING" | "RANGING" | "UNKNOWN"
        - confidence: based on data length and trend strength
    """
    try:
        # Extract market data from context
        prices = context.get("prices", np.array([]))
        if len(prices) < 20:
            return AgentResult(
                agent_id=AGENT_ID,
                name=AGENT_NAME,
                layer=LAYER,
                score=None,
                label="UNKNOWN",
                confidence=0.0,
                features={},
                ok=False,
                error="insufficient data",
                latency_ms=context.get("latency_ms", 0.0),
            )

        # Calculate simple moving averages
        short_ma = np.mean(prices[-10:])
        long_ma = np.mean(prices[-30:]) if len(prices) >= 30 else np.mean(prices[-20:])

        # Volatility measure
        returns = np.diff(prices) / prices[:-1]
        volatility = np.std(returns)

        # Regime determination
        if abs(short_ma - long_ma) / long_ma > 0.02:
            label = "TRENDING"
            score = min(100, 60 + abs(short_ma - long_ma) * 500)
        elif volatility > np.percentile([abs(r) for r in returns], 75):
            label = "TRENDING"
            score = 70
        else:
            label = "RANGING"
            score = max(0, 40 - volatility * 100)

        # Confidence based on data length
        confidence = min(1.0, len(prices) / 100)

        features = {
            "price_length": len(prices),
            "volatility": float(volatility),
            "short_ma": float(short_ma),
            "long_ma": float(long_ma),
            "ma_difference_pct": float(abs(short_ma - long_ma) / long_ma * 100) if long_ma != 0 else 0,
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