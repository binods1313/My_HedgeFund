"""02 TechnicalAgent - price-action patterns, EMA ribbon, RSI, MACD"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


def _ema_last(prices: np.ndarray, period: int) -> float:
    """Last EMA value. The previous convolve used a missing numpy name."""
    alpha = 2.0 / (period + 1.0)
    value = float(prices[0])
    for price in prices[1:]:
        value = alpha * float(price) + (1.0 - alpha) * value
    return value


AGENT_ID = "02"
AGENT_NAME = "Technical Agent"
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
    """Analyze technical indicators: EMA ribbon, RSI, MACD.

    Returns:
        - score: 0-100 based on indicator alignment
        - label: "BULLISH" | "BEARISH" | "NEUTRAL"
        - confidence: based on indicator strength and convergence
    """
    try:
        prices = context.get("prices", np.array([]))
        if len(prices) < 30:
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

        # EMA ribbon. A scalar series has no signal line to smooth.
        ema_fast = _ema_last(prices, 5)
        ema_slow = _ema_last(prices, 20)

        # RSI (14)
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.convolve(gains, np.ones(14)/14, mode='valid')[-1] if len(prices) >= 15 else np.mean(gains[-14:]) if len(gains) >= 14 else np.mean(gains)
        avg_loss = np.convolve(losses, np.ones(14)/14, mode='valid')[-1] if len(prices) >= 15 else np.mean(losses[-14:]) if len(losses) >= 14 else np.mean(losses)
        rs = avg_gain / avg_loss if avg_loss != 0 else 100
        rsi = 100 - (100 / (1 + rs))

        # MACD from the same EMA. One value has no 9-bar signal series.
        ema_12 = _ema_last(prices, 12)
        ema_26 = _ema_last(prices, 26)
        macd = ema_12 - ema_26
        signal_line = macd
        histogram = macd - signal_line

        # Trend determination
        bullish_signals = sum([
            ema_fast > ema_slow,
            rsi > 50,
            macd > 0,
            histogram > 0,
        ])
        bearish_signals = 4 - bullish_signals

        if bullish_signals >= 3:
            label = "BULLISH"
            score = min(100, 50 + bullish_signals * 12.5)
        elif bearish_signals >= 3:
            label = "BEARISH"
            score = max(0, 50 - bearish_signals * 12.5)
        else:
            label = "NEUTRAL"
            score = 50

        confidence = min(1.0, len(prices) / 50)

        features = {
            "price_length": len(prices),
            "ema_fast": float(ema_fast),
            "ema_slow": float(ema_slow),
            "rsi": float(rsi),
            "macd": float(macd),
            "histogram": float(histogram),
            "bullish_signals": bullish_signals,
            "bearish_signals": bearish_signals,
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