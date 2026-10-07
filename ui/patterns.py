"""Closed-bar candlestick names for the chart.

Eleven shapes: three doji forms, two engulfing pairs, two marubozu directions,
and the hammer family split by the prior swing (hammer / hanging man,
inverted hammer / shooting star). Index i uses only bars 0..i. The caller
passes ``upto`` as one past the last closed bar, matching the indicator
signals. These hits are display data. They are not sent to the engine.
"""

from __future__ import annotations

from dataclasses import dataclass

PATTERN_NAMES = (
    "Dragonfly Doji",
    "Gravestone Doji",
    "Doji",
    "Bullish Engulfing",
    "Bearish Engulfing",
    "Bullish Marubozu",
    "Bearish Marubozu",
    "Hammer",
    "Hanging Man",
    "Inverted Hammer",
    "Shooting Star",
)


@dataclass(frozen=True)
class PatternHit:
    index: int
    name: str
    bias: str
    sl: float | None
    tp: float | None


# Chart marks stay short so the candle is still readable.
MARKER_LABELS = {
    "Dragonfly Doji": "DDj",
    "Gravestone Doji": "GDj",
    "Doji": "DJ",
    "Bullish Engulfing": "BEng",
    "Bearish Engulfing": "BrEng",
    "Bullish Marubozu": "BMaru",
    "Bearish Marubozu": "BrMaru",
    "Hammer": "Ham",
    "Hanging Man": "HMan",
    "Inverted Hammer": "IHam",
    "Shooting Star": "SStar",
}

PATTERN_GROUPS = {
    "Dragonfly Doji": "Indecision",
    "Gravestone Doji": "Indecision",
    "Doji": "Indecision",
    "Bullish Engulfing": "Reversals",
    "Bearish Engulfing": "Reversals",
    "Bullish Marubozu": "Momentum",
    "Bearish Marubozu": "Momentum",
    "Hammer": "Reversals",
    "Hanging Man": "Reversals",
    "Inverted Hammer": "Reversals",
    "Shooting Star": "Reversals",
}

SENTIMENT = {
    "Dragonfly Doji": "Bullish rejection. A long lower wick and a small body.",
    "Gravestone Doji": "Bearish rejection. A long upper wick and a small body.",
    "Doji": "Indecision. The close finished near the open.",
    "Bullish Engulfing": "Bullish reversal. This body covers the prior down bar.",
    "Bearish Engulfing": "Bearish reversal. This body covers the prior up bar.",
    "Bullish Marubozu": "Bullish continuation. The body fills almost the whole range.",
    "Bearish Marubozu": "Bearish continuation. The body fills almost the whole range.",
    "Hammer": "Bullish reversal. A long lower wick after a falling swing.",
    "Hanging Man": "Bearish reversal. A long lower wick after a rising swing.",
    "Inverted Hammer": "Bullish reversal. A long upper wick after a falling swing.",
    "Shooting Star": "Bearish reversal. A long upper wick after a rising swing.",
}

FILTER_CHOICES = (
    "All patterns",
    "Reversals only",
    "Indecision only",
    "Momentum only",
    "Hide all",
)

_FILTER_GROUP = {
    "Reversals only": "Reversals",
    "Indecision only": "Indecision",
    "Momentum only": "Momentum",
}


def marker_label(name: str) -> str:
    return MARKER_LABELS.get(name, name[:4])


def pattern_group(name: str) -> str:
    return PATTERN_GROUPS.get(name, "Other")


def sentiment_text(name: str) -> str:
    return SENTIMENT.get(name, "Unclassified shape.")


def allows_pattern(name: str, choice: str) -> bool:
    if choice == "Hide all":
        return False
    group = _FILTER_GROUP.get(choice)
    if group is None:
        return True
    return pattern_group(name) == group


def pattern_key(when: str, name: str) -> str:
    raw = f"{when}-{name}"
    safe = "".join(ch if ch.isalnum() else "-" for ch in raw)
    return safe[:80]


def describe_trigger(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    index: int,
    name: str,
) -> str:
    """State the same thresholds the classifier uses, from this bar's OHLC."""
    if index < 0 or index >= len(closes):
        return "Bar is outside the closed window."
    open_, high, low, close = opens[index], highs[index], lows[index], closes[index]
    span = high - low
    if span <= 0:
        return "Range is zero, so no threshold applies."
    body = abs(close - open_)
    upper = high - max(open_, close)
    lower = min(open_, close) - low
    body_share = _share(body, span)
    upper_share = _share(upper, span)
    lower_share = _share(lower, span)
    if name == "Dragonfly Doji":
        return (
            f"Open {open_:.5f}, high {high:.5f}, low {low:.5f}, close {close:.5f}. "
            f"Body is {body_share} of the range (doji limit 10%). "
            f"Lower wick is {lower_share} (needs at least 65%). "
            f"Upper wick is {upper_share} (needs at most 10%)."
        )
    if name == "Gravestone Doji":
        return (
            f"Open {open_:.5f}, high {high:.5f}, low {low:.5f}, close {close:.5f}. "
            f"Body is {body_share} of the range (doji limit 10%). "
            f"Upper wick is {upper_share} (needs at least 65%). "
            f"Lower wick is {lower_share} (needs at most 10%)."
        )
    if name == "Doji":
        return (
            f"Open {open_:.5f}, high {high:.5f}, low {low:.5f}, close {close:.5f}. "
            f"Body is {body_share} of the range. Doji needs the body at or under 10%."
        )
    if name in {"Bullish Engulfing", "Bearish Engulfing"} and index >= 1:
        prior_open, prior_close = opens[index - 1], closes[index - 1]
        return (
            f"Prior open {prior_open:.5f}, prior close {prior_close:.5f}. "
            f"This open {open_:.5f}, close {close:.5f}. "
            "Engulfing needs the body to cover the prior body."
        )
    if name in {"Bullish Marubozu", "Bearish Marubozu"}:
        return (
            f"Open {open_:.5f}, high {high:.5f}, low {low:.5f}, close {close:.5f}. "
            f"Body is {body_share} of the range. Marubozu needs at least 90%."
        )
    if body <= 0:
        return "Body is zero, so the wick ratio is undefined."
    wick_multiple = (lower if name in {"Hammer", "Hanging Man"} else upper) / body
    wick_name = "Lower" if name in {"Hammer", "Hanging Man"} else "Upper"
    swing = "falling" if name in {"Hammer", "Inverted Hammer"} else "rising"
    return (
        f"Open {open_:.5f}, high {high:.5f}, low {low:.5f}, close {close:.5f}. "
        f"{wick_name} wick is {wick_multiple:.1f}× the body (needs at least 2×) "
        f"and the prior swing is {swing}."
    )


def _share(part: float, whole: float) -> str:
    return f"{(part / whole) * 100:.1f}%"


def detect_patterns(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    *,
    upto: int,
    rr: float = 3.0,
) -> list[PatternHit]:
    """Return at most one pattern per closed bar, oldest first."""
    count = min(len(opens), len(highs), len(lows), len(closes), max(0, upto))
    if count < 1 or rr <= 0:
        return []
    found: list[PatternHit] = []
    for index in range(count):
        hit = _classify(opens, highs, lows, closes, index, rr)
        if hit is not None:
            found.append(hit)
    return found


def _classify(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    index: int,
    rr: float,
) -> PatternHit | None:
    open_, high, low, close = opens[index], highs[index], lows[index], closes[index]
    span = high - low
    if span <= 0:
        return None
    body = abs(close - open_)
    upper = high - max(open_, close)
    lower = min(open_, close) - low
    bullish = close > open_
    bearish = close < open_

    if body <= span * 0.10 and lower >= span * 0.65 and upper <= span * 0.10:
        return _hit(index, "Dragonfly Doji", "bullish", close, low, high, rr)
    if body <= span * 0.10 and upper >= span * 0.65 and lower <= span * 0.10:
        return _hit(index, "Gravestone Doji", "bearish", close, low, high, rr)
    if body <= span * 0.10:
        return _hit(index, "Doji", "neutral", close, low, high, rr)

    if index >= 1:
        prev_open, prev_close = opens[index - 1], closes[index - 1]
        if prev_close < prev_open and bullish and open_ <= prev_close and close >= prev_open:
            return _hit(index, "Bullish Engulfing", "bullish", close, low, high, rr)
        if prev_close > prev_open and bearish and open_ >= prev_close and close <= prev_open:
            return _hit(index, "Bearish Engulfing", "bearish", close, low, high, rr)

    if body >= span * 0.90 and bullish:
        return _hit(index, "Bullish Marubozu", "bullish", close, low, high, rr)
    if body >= span * 0.90 and bearish:
        return _hit(index, "Bearish Marubozu", "bearish", close, low, high, rr)

    if body <= 0:
        return None
    long_lower = lower >= body * 2.0 and upper <= body * 0.5 and lower >= span * 0.55
    long_upper = upper >= body * 2.0 and lower <= body * 0.5 and upper >= span * 0.55
    if long_lower and _falling(closes, index):
        return _hit(index, "Hammer", "bullish", close, low, high, rr)
    if long_lower and _rising(closes, index):
        return _hit(index, "Hanging Man", "bearish", close, low, high, rr)
    if long_upper and _falling(closes, index):
        return _hit(index, "Inverted Hammer", "bullish", close, low, high, rr)
    if long_upper and _rising(closes, index):
        return _hit(index, "Shooting Star", "bearish", close, low, high, rr)
    return None


def _falling(closes: list[float], index: int) -> bool:
    return index >= 3 and closes[index - 1] < closes[index - 3]


def _rising(closes: list[float], index: int) -> bool:
    return index >= 3 and closes[index - 1] > closes[index - 3]


def _hit(
    index: int,
    name: str,
    bias: str,
    entry: float,
    low: float,
    high: float,
    rr: float,
) -> PatternHit:
    if bias == "neutral":
        return PatternHit(index, name, bias, None, None)
    if bias == "bullish":
        distance = entry - low
        extreme = low
        take = entry + rr * distance
    else:
        distance = high - entry
        extreme = high
        take = entry - rr * distance
    if distance <= 0:
        return PatternHit(index, name, bias, None, None)
    return PatternHit(index, name, bias, extreme, take)
