"""Closed-bar indicator math for the chart.

engine.indicators exposes structure tools and a single ema_ribbon snapshot.
It does not provide RSI, MACD, ATR, or Bollinger series, and ema_ribbon
refuses short samples. These functions are the chart series. Each value at
index i uses only rows 0..i.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

INDICATOR_CHOICES = ("EMA20", "EMA50", "RSI", "MACD", "ATR", "Bollinger")
DEFAULT_INDICATORS = ("EMA20", "EMA50")
MAX_INDICATORS = 4


def clamp_indicators(selected: list[str] | tuple[str, ...] | None, *, limit: int = MAX_INDICATORS) -> list[str]:
    """Keep the first unique known names, never more than ``limit``."""
    if not selected:
        return list(DEFAULT_INDICATORS)
    chosen: list[str] = []
    for name in selected:
        if name in INDICATOR_CHOICES and name not in chosen:
            chosen.append(name)
        if len(chosen) >= limit:
            break
    return chosen or list(DEFAULT_INDICATORS)


def ema(values: list[float], span: int) -> list[float | None]:
    if span < 1:
        raise ValueError("span must be positive")
    out: list[float | None] = [None] * len(values)
    if len(values) < span:
        return out
    seed = sum(values[:span]) / span
    out[span - 1] = seed
    k = 2.0 / (span + 1.0)
    prev = seed
    for index in range(span, len(values)):
        prev = values[index] * k + prev * (1.0 - k)
        out[index] = prev
    return out


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period < 1 or len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[index] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(
    values: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    fast_line = ema(values, fast)
    slow_line = ema(values, slow)
    line: list[float | None] = [
        None if left is None or right is None else left - right
        for left, right in zip(fast_line, slow_line)
    ]
    signal_line: list[float | None] = [None] * len(values)
    histogram: list[float | None] = [None] * len(values)
    valid = [index for index, value in enumerate(line) if value is not None]
    if len(valid) < signal:
        return line, signal_line, histogram
    series = [float(line[index]) for index in valid]
    smoothed = ema(series, signal)
    for offset, index in enumerate(valid):
        signal_line[index] = smoothed[offset]
        if smoothed[offset] is not None and line[index] is not None:
            histogram[index] = float(line[index]) - float(smoothed[offset])
    return line, signal_line, histogram


def atr(high: list[float], low: list[float], close: list[float], period: int = 14) -> list[float | None]:
    count = len(close)
    out: list[float | None] = [None] * count
    if period < 1 or count < period or not (len(high) == count and len(low) == count):
        return out
    true_range = [_true_range(high, low, close, index) for index in range(count)]
    seed = sum(true_range[:period]) / period
    out[period - 1] = seed
    prev = seed
    for index in range(period, count):
        prev = (prev * (period - 1) + true_range[index]) / period
        out[index] = prev
    return out


def _true_range(high: list[float], low: list[float], close: list[float], index: int) -> float:
    span = high[index] - low[index]
    if index == 0:
        return span
    return max(span, abs(high[index] - close[index - 1]), abs(low[index] - close[index - 1]))


def bollinger(
    values: list[float], period: int = 20, width: float = 2.0
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    count = len(values)
    mid: list[float | None] = [None] * count
    upper: list[float | None] = [None] * count
    lower: list[float | None] = [None] * count
    if period < 1 or count < period:
        return mid, upper, lower
    for index in range(period - 1, count):
        window = values[index - period + 1 : index + 1]
        mean = sum(window) / period
        variance = sum((item - mean) ** 2 for item in window) / period
        sigma = math.sqrt(variance)
        mid[index] = mean
        upper[index] = mean + width * sigma
        lower[index] = mean - width * sigma
    return mid, upper, lower


def closed_only(count: int) -> int:
    """Number of rows after the forming bar is dropped. The last row is forming."""
    return max(0, count - 1)


@dataclass(frozen=True)
class Signal:
    index: int
    side: str
    reason: str
    source: str


def ema_cross_signals(fast: list[float | None], slow: list[float | None], *, upto: int) -> list[Signal]:
    return _cross_signals(fast, slow, upto, "EMA", "EMA fast crossed above slow", "EMA fast crossed below slow")


def macd_cross_signals(line: list[float | None], signal: list[float | None], *, upto: int) -> list[Signal]:
    return _cross_signals(line, signal, upto, "MACD", "MACD crossed above its signal", "MACD crossed below its signal")


def _cross_signals(
    fast: list[float | None],
    slow: list[float | None],
    upto: int,
    source: str,
    buy_reason: str,
    sell_reason: str,
) -> list[Signal]:
    found: list[Signal] = []
    last = min(upto, len(fast), len(slow))
    for index in range(1, last):
        prev_fast, prev_slow = fast[index - 1], slow[index - 1]
        now_fast, now_slow = fast[index], slow[index]
        if None in (prev_fast, prev_slow, now_fast, now_slow):
            continue
        if float(prev_fast) <= float(prev_slow) and float(now_fast) > float(now_slow):
            found.append(Signal(index, "BUY", buy_reason, source))
        elif float(prev_fast) >= float(prev_slow) and float(now_fast) < float(now_slow):
            found.append(Signal(index, "SELL", sell_reason, source))
    return found


def rsi_signals(values: list[float | None], *, upto: int) -> list[Signal]:
    found: list[Signal] = []
    last = min(upto, len(values))
    for index in range(1, last):
        prev, now = values[index - 1], values[index]
        if prev is None or now is None:
            continue
        if prev < 30.0 <= now:
            found.append(Signal(index, "BUY", "RSI left oversold", "RSI"))
        elif prev > 70.0 >= now:
            found.append(Signal(index, "SELL", "RSI left overbought", "RSI"))
    return found


def bollinger_signals(
    close: list[float],
    upper: list[float | None],
    lower: list[float | None],
    *,
    upto: int,
) -> list[Signal]:
    found: list[Signal] = []
    last = min(upto, len(close), len(upper), len(lower))
    for index in range(1, last):
        if None in (upper[index], lower[index], upper[index - 1], lower[index - 1]):
            continue
        prev_close = close[index - 1]
        now = close[index]
        prev_upper = float(upper[index - 1])
        prev_lower = float(lower[index - 1])
        now_upper = float(upper[index])
        now_lower = float(lower[index])
        inside = now_lower <= now <= now_upper
        if prev_close > prev_upper and inside:
            found.append(Signal(index, "SELL", "Close came back inside after an upper break", "Bollinger"))
        elif prev_close < prev_lower and inside:
            found.append(Signal(index, "BUY", "Close came back inside after a lower break", "Bollinger"))
    return found


def round_to_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    steps = round(price / tick_size)
    return steps * tick_size


@dataclass(frozen=True)
class StopPlan:
    ok: bool
    side: str
    entry: float
    sl: float | None
    tp: float | None
    reason: str


def plan_stops(
    side: str,
    entry: float,
    atr_value: float | None,
    *,
    atr_k: float,
    rr: float,
    tick_size: float,
    stops_level_points: float = 0.0,
    point: float = 0.0,
) -> StopPlan:
    """SL is atr_k × ATR. TP is rr × that distance. ATR is not a direction."""
    if side not in {"BUY", "SELL"}:
        return StopPlan(False, side, entry, None, None, "Side must be BUY or SELL")
    if atr_value is None or not math.isfinite(atr_value) or atr_value <= 0:
        return StopPlan(False, side, entry, None, None, "ATR is unavailable on the closed bar")
    if not math.isfinite(entry) or entry <= 0:
        return StopPlan(False, side, entry, None, None, "Entry price is unavailable")
    distance = atr_k * atr_value
    minimum = max(0.0, stops_level_points) * max(0.0, point)
    if minimum > 0 and distance + 1e-12 < minimum:
        return StopPlan(
            False,
            side,
            entry,
            None,
            None,
            f"ATR stop {distance:.8f} is inside stops_level {minimum:.8f}",
        )
    if side == "BUY":
        sl = round_to_tick(entry - distance, tick_size)
        tp = round_to_tick(entry + rr * (entry - sl), tick_size)
        actual = entry - sl
    else:
        sl = round_to_tick(entry + distance, tick_size)
        tp = round_to_tick(entry - rr * (sl - entry), tick_size)
        actual = sl - entry
    if sl <= 0 or tp <= 0:
        return StopPlan(False, side, entry, None, None, "Rounded stop or target is not a positive price")
    if minimum > 0 and actual + (tick_size / 2.0) < minimum:
        return StopPlan(
            False,
            side,
            entry,
            None,
            None,
            f"Rounded stop is inside stops_level {minimum:.8f}",
        )
    return StopPlan(True, side, entry, sl, tp, "ATR stop and RR target")


def format_fixed(value: object, digits: int) -> str:
    """Fixed decimals. Never scientific notation."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    places = max(0, min(int(digits), 10))
    return f"{number:.{places}f}"


def change_24h_label(closes: list[float] | None, current: float | None) -> str | None:
    """Previous completed D1 close versus the current price. None if D1 is missing."""
    if not closes or len(closes) < 2 or current is None:
        return None
    previous = closes[-2]
    if previous is None or not math.isfinite(previous) or previous == 0:
        return None
    if not math.isfinite(current):
        return None
    pct = (current - previous) / previous * 100.0
    return f"{pct:+.2f}%"
