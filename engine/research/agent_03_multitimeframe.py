"""03 MultiTimeframeAgent - alignment vector across timeframes"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


AGENT_ID = "03"
AGENT_NAME = "Multi-Timeframe Agent"
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
    """Analyze alignment across multiple timeframes (M1, M5, H1, H4, D1).

    Returns:
        - score: -1 (bearish alignment) to +1 (bullish alignment)
        - label: "BULLISH_ALIGNMENT" | "BEARISH_ALIGNMENT" | "MIXED"
        - confidence: based on number of timeframes agreeing
    """
    try:
        prices_by_tf = context.get("prices_by_tf", {})
        if not prices_by_tf or len(prices_by_tf) < 2:
            return AgentResult(
                agent_id=AGENT_ID,
                name=AGENT_NAME,
                layer=LAYER,
                score=None,
                label="UNKNOWN",
                confidence=0.0,
                features={},
                ok=False,
                error="no timeframe data",
                latency_ms=context.get("latency_ms", 0.0),
            )

        alignment_count = 0
        total_tf = 0
        signals: dict[str, str] = {}

        for tf, prices in prices_by_tf.items():
            if len(prices) < 20:
                continue
            total_tf += 1

            # Calculate trend for this timeframe
            short_ma = np.mean(prices[-10:])
            long_ma = np.mean(prices[-50:]) if len(prices) >= 50 else np.mean(prices[-20:])

            if abs(short_ma - long_ma) / long_ma > 0.02:
                # Strong trend
                if short_ma > long_ma:
                    signals[tf] = "BULLISH"
                    alignment_count += 1
                else:
                    signals[tf] = "BEARISH"
            else:
                # Ranging
                signals[tf] = "RANGING"

        if alignment_count == 0:
            label = "MIXED"
            score = 0.0
        elif alignment_count / total_tf >= 0.75:
            label = "BULLISH_ALIGNMENT" if alignment_count > total_tf - alignment_count else "BEARISH_ALIGNMENT"
            score = alignment_count / total_tf
        else:
            label = "MIXED"
            score = alignment_count / total_tf

        confidence = alignment_count / total_tf if total_tf > 0 else 0.0

        features = {
            "timeframe_count": total_tf,
            "aligned_count": alignment_count,
            "signals": signals,
            "score_details": {tf: "BULLISH" if short_ma > long_ma else "BEARISH" if short_ma < long_ma else "RANGING"
                             for tf, short_ma, long_ma in [
                                 (tf, np.mean(prices[-10:]) if len(prices) >= 10 else 0,
                                  np.mean(prices[-50:]) if len(prices) >= 50 else np.mean(prices[-20:]))
                                 for tf, prices in prices_by_tf.items()
                             ]},
        }

        return AgentResult(
            agent_id=AGENT_ID,
            name=AGENT_NAME,
            layer=LAYER,
            score=round(score, 3),
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