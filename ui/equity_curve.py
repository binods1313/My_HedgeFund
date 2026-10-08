"""Drop bogus equity points before the chart draws them."""

from __future__ import annotations

import math
from datetime import datetime


def _epoch(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def clean_equity_points(
    timestamps: list[object],
    equities: list[object],
    *,
    max_gap_s: float = 1800.0,
) -> list[tuple[float, float]]:
    """Drop equity <= 0, keep the latest contiguous segment, then drop cliffs.

    A gap longer than ``max_gap_s`` is a disconnected snapshot. The fill must
    not be drawn back to zero. A point outside half to double the segment
    median is a cliff and is left out when at least two points remain.
    """
    rows: list[tuple[float, float]] = []
    for stamp, equity in zip(timestamps, equities):
        when = _epoch(stamp)
        try:
            amount = float(equity)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if when is None or not math.isfinite(amount) or amount <= 0:
            continue
        rows.append((when, amount))
    rows.sort()
    if not rows:
        return []
    segments: list[list[tuple[float, float]]] = [[rows[0]]]
    for when, amount in rows[1:]:
        if when - segments[-1][-1][0] > max_gap_s:
            segments.append([])
        segments[-1].append((when, amount))
    return drop_cliff_points(segments[-1])


def drop_cliff_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop snapshots far from the median so one bad print cannot cliff the chart.

    A segment shorter than three points is returned as-is. If the filter would
    leave fewer than two points, the unfiltered segment is kept.
    """
    if len(points) < 3:
        return list(points)
    amounts = sorted(point[1] for point in points)
    mid = len(amounts) // 2
    if len(amounts) % 2:
        median = amounts[mid]
    else:
        median = (amounts[mid - 1] + amounts[mid]) / 2.0
    if median <= 0:
        return list(points)
    kept = [point for point in points if median * 0.5 <= point[1] <= median * 2.0]
    if len(kept) < 2:
        return list(points)
    return kept


MIN_VISIBLE_FRACTION = 0.0005


def equity_axis_range(low: float, high: float) -> tuple[float, float]:
    """Y window at least 0.05% of the equity level, so a small move stays small."""
    if not math.isfinite(low) or not math.isfinite(high):
        return 0.0, 1.0
    if high < low:
        low, high = high, low
    mid = (low + high) / 2.0
    span = max((high - low) * 1.16, abs(mid) * MIN_VISIBLE_FRACTION, 1.0)
    return mid - span / 2.0, mid + span / 2.0


def equity_move_caption(first: float, last: float, currency: str | None) -> str:
    """Euro amount and percent over the drawn window. Separate from the cliff sentence."""
    from formatting import format_money

    if not math.isfinite(first) or not math.isfinite(last) or first == 0:
        return "— · — over the window"
    delta = last - first
    percent = delta / first * 100.0
    return f"{format_money(delta, currency)} · {percent:+.2f}% over the window"


def equity_caption(first: float, last: float) -> str:
    """Plain sentence. No raw equity series."""
    if last > first:
        verb = "rose"
    elif last < first:
        verb = "fell"
    else:
        verb = "was unchanged"
    return (
        f"Equity {verb} from the first snapshot to the latest in the current "
        "continuous window. Gaps longer than thirty minutes and non-positive or "
        "cliff snapshots are left out."
    )
