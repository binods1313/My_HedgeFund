"""Pure calibration math. Nothing here reads a network or places an order."""

from __future__ import annotations

import math


def wilson_interval(hits: int, count: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval for a binomial hit rate. None when count is zero."""
    if count <= 0 or hits < 0 or hits > count:
        return None
    proportion = hits / count
    z2 = z * z
    denominator = 1.0 + z2 / count
    centre = (proportion + z2 / (2.0 * count)) / denominator
    margin = z * math.sqrt((proportion * (1.0 - proportion) + z2 / (4.0 * count)) / count) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _predicted(score: float) -> float | None:
    if not math.isfinite(score):
        return None
    if -1.0 <= score <= 1.0:
        return (score + 1.0) / 2.0
    if 0.0 <= score <= 100.0:
        return score / 100.0
    return None


def _bucket_start(score: float) -> float | None:
    predicted = _predicted(score)
    if predicted is None:
        return None
    scaled = predicted * 100.0
    start = math.floor(scaled / 20.0) * 20.0
    return min(80.0, max(0.0, start))


def summarise(samples: list[dict], *, minimum: int = 30) -> dict:
    """Hit rate, Wilson interval, mean R, and reliability gap for one group."""
    decided = [row for row in samples if row.get("hit") in (0, 1)]
    count = len(decided)
    hits = sum(int(row["hit"]) for row in decided)
    rs = [float(row["r"]) for row in samples if isinstance(row.get("r"), (int, float)) and math.isfinite(float(row["r"]))]
    mean_r = sum(rs) / len(rs) if rs else None
    interval = wilson_interval(hits, count)
    predicted = []
    for row in decided:
        score = row.get("score")
        if isinstance(score, (int, float)):
            value = _predicted(float(score))
            if value is not None:
                predicted.append(value)
    hit_rate = hits / count if count else None
    gap = None
    if predicted and hit_rate is not None:
        gap = (sum(predicted) / len(predicted)) - hit_rate
    return {
        "count": count,
        "hits": hits,
        "hit_rate": hit_rate,
        "low": None if interval is None else interval[0],
        "high": None if interval is None else interval[1],
        "mean_r": mean_r,
        "reliability_gap": gap,
        "insufficient": count < minimum,
    }


def by_bucket(samples: list[dict], *, minimum: int = 30) -> list[dict]:
    grouped: dict[float, list[dict]] = {}
    for row in samples:
        score = row.get("score")
        if not isinstance(score, (int, float)):
            continue
        start = _bucket_start(float(score))
        if start is None:
            continue
        grouped.setdefault(start, []).append(row)
    rows = []
    for start in sorted(grouped):
        summary = summarise(grouped[start], minimum=minimum)
        summary["bucket"] = f"{int(start)}-{int(start + 20)}"
        rows.append(summary)
    return rows


def drift(samples: list[dict], window: int, *, minimum: int = 30) -> dict:
    """Latest resolved window against the rows before it."""
    decided = [row for row in samples if row.get("hit") in (0, 1)]
    if window < 1 or len(decided) <= window:
        return {"insufficient": True, "latest_hit_rate": None, "history_hit_rate": None, "delta": None}
    latest = summarise(decided[-window:], minimum=minimum)
    history = summarise(decided[:-window], minimum=minimum)
    delta = None
    if latest["hit_rate"] is not None and history["hit_rate"] is not None:
        delta = latest["hit_rate"] - history["hit_rate"]
    return {
        "insufficient": bool(latest["insufficient"] or history["insufficient"]),
        "latest_hit_rate": latest["hit_rate"],
        "history_hit_rate": history["hit_rate"],
        "delta": delta,
    }


def had_edge(samples: list[dict], *, minimum: int = 30) -> bool | None:
    """True when a large enough sample has positive mean R. None when it is too small."""
    summary = summarise(samples, minimum=minimum)
    if summary["insufficient"] or summary["mean_r"] is None:
        return None
    return bool(summary["mean_r"] > 0)
