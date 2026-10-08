"""Equity-snapshot statistics for the Portfolio view.

Win/loss stays absent until a realised P&L series is passed in. These
numbers are not broker returns and they do not change an order.
"""

from __future__ import annotations

import math

MIN_SHARPE_POINTS = 30
SNAPSHOTS_PER_YEAR = 252
ANNUALISATION = (
    "Sharpe annualises the standard deviation of simple returns between successive "
    "equity snapshots with sqrt(252). Each step is one snapshot, not one session close. "
    "from equity snapshots."
)
LABEL = "from equity snapshots"


def slice_window(values: list[float], window: str) -> list[float]:
    if window == "Last 30":
        return list(values[-30:])
    if window == "Last 7":
        return list(values[-7:])
    return list(values)


def equity_change(values: list[float]) -> tuple[float, float] | None:
    if len(values) < 2 or not math.isfinite(values[0]) or values[0] == 0:
        return None
    if not math.isfinite(values[-1]):
        return None
    delta = values[-1] - values[0]
    return delta, delta / values[0] * 100.0


def max_drawdown_pct(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    peak = values[0]
    worst = 0.0
    for value in values:
        if not math.isfinite(value):
            continue
        if value > peak:
            peak = value
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100.0)
    return worst


def sharpe_from_snapshots(values: list[float]) -> float | None:
    """None until 30 finite positive snapshots exist. Annualised with sqrt(252)."""
    if len(values) < MIN_SHARPE_POINTS:
        return None
    returns: list[float] = []
    for previous, nxt in zip(values, values[1:]):
        if not math.isfinite(previous) or not math.isfinite(nxt) or previous <= 0:
            return None
        returns.append((nxt - previous) / previous)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((item - mean) ** 2 for item in returns) / (len(returns) - 1)
    if variance <= 0:
        return None
    return (mean / math.sqrt(variance)) * math.sqrt(SNAPSHOTS_PER_YEAR)


def win_loss_ratio(realised: list[float] | None) -> float | None:
    if not realised:
        return None
    wins = sum(1 for item in realised if math.isfinite(item) and item > 0)
    losses = sum(1 for item in realised if math.isfinite(item) and item < 0)
    if losses == 0:
        return None
    return wins / losses


def performance_report(
    values: list[float],
    *,
    realised: list[float] | None = None,
) -> dict[str, object]:
    report: dict[str, object] = {
        "change": equity_change(values),
        "max_drawdown_pct": max_drawdown_pct(values),
        "sharpe": sharpe_from_snapshots(values),
        "label": LABEL,
        "annualisation": ANNUALISATION,
        "count": len(values),
    }
    if realised is not None:
        report["win_loss"] = win_loss_ratio(realised)
    return report
