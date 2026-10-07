"""Resolve a shadow prediction from bars that already closed.

ATR is the chart's Wilder average, computed only on bars up to the signal bar.
A bar where both the stop and the target are touched counts as a stop. Spread,
when the bar carries one, is a price distance added against the entry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from learning.bars import Bar


@dataclass(frozen=True)
class Resolution:
    forward_return: float | None
    hit: int | None
    mfe: float | None
    mae: float | None
    r_multiple: float | None


def _true_range(bars: list[Bar], index: int) -> float:
    span = bars[index].high - bars[index].low
    if index == 0:
        return span
    previous = bars[index - 1].close
    return max(span, abs(bars[index].high - previous), abs(bars[index].low - previous))


def wilder_atr(bars: list[Bar], period: int) -> list[float | None]:
    """Same Wilder smoothing as the chart ATR. No bar after `period` is required."""
    count = len(bars)
    out: list[float | None] = [None] * count
    if period < 1 or count < period:
        return out
    ranges = [_true_range(bars, index) for index in range(count)]
    seed = sum(ranges[:period]) / period
    out[period - 1] = seed
    previous = seed
    for index in range(period, count):
        previous = (previous * (period - 1) + ranges[index]) / period
        out[index] = previous
    return out


def resolve_trade(
    bars: list[Bar],
    entry_index: int,
    direction: str,
    horizon: int,
    *,
    atr_k: float,
    rr: float,
    atr_period: int,
) -> Resolution | None:
    """Outcome of one horizon. None when the future bars are not closed yet."""
    if direction not in {"long", "short"}:
        return None
    if horizon < 1 or entry_index < 0 or entry_index >= len(bars):
        return None
    end = entry_index + horizon
    if end >= len(bars):
        return None
    history = bars[: entry_index + 1]
    atr_values = wilder_atr(history, atr_period)
    atr_value = atr_values[-1] if atr_values else None
    entry = bars[entry_index].close
    spread = bars[entry_index].spread or 0.0
    if spread < 0 or not math.isfinite(spread):
        spread = 0.0
    fill = entry + spread if direction == "long" else entry - spread
    if not math.isfinite(fill) or fill <= 0:
        return None
    window = bars[entry_index + 1 : end + 1]
    if direction == "long":
        mfe = max(bar.high - fill for bar in window)
        mae = max(fill - bar.low for bar in window)
    else:
        mfe = max(fill - bar.low for bar in window)
        mae = max(bar.high - fill for bar in window)
    exit_close = window[-1].close
    if direction == "long":
        forward = (exit_close - fill) / fill
    else:
        forward = (fill - exit_close) / fill
    if atr_value is None or not math.isfinite(atr_value) or atr_value <= 0 or atr_k <= 0 or rr < 1:
        return Resolution(forward, None, mfe, mae, None)
    distance = atr_k * atr_value
    if direction == "long":
        stop = fill - distance
        target = fill + rr * distance
    else:
        stop = fill + distance
        target = fill - rr * distance
    outcome = "neither"
    for bar in window:
        if direction == "long":
            stopped = bar.low <= stop
            targeted = bar.high >= target
        else:
            stopped = bar.high >= stop
            targeted = bar.low <= target
        if stopped:
            outcome = "stop"
            break
        if targeted:
            outcome = "target"
            break
    if outcome == "target":
        return Resolution(forward, 1, mfe, mae, rr)
    if outcome == "stop":
        return Resolution(forward, 0, mfe, mae, -1.0)
    mark = ((exit_close - fill) if direction == "long" else (fill - exit_close)) / distance
    return Resolution(forward, None, mfe, mae, mark)
