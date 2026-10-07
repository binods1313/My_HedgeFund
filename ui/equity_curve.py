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
    """Drop equity <= 0 and keep only the latest contiguous segment.

    A gap longer than ``max_gap_s`` is a disconnected snapshot. The fill must
    not be drawn back to zero.
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
    return segments[-1]
