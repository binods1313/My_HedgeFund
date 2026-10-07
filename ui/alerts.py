"""Read-only risk banners. Nothing here submits or closes an order."""

from __future__ import annotations

import math

# Same warn level the pre-submit check already uses. config.yaml has no
# separate margin-level percent.
MARGIN_WARN_LEVEL = 100.0


def alert_signature(
    tickets: list[int],
    *,
    margin_low: bool,
    risk_high: bool,
) -> str | None:
    parts: list[str] = []
    if tickets:
        parts.append("sl:" + ",".join(str(ticket) for ticket in sorted(set(tickets))))
    if margin_low:
        parts.append("margin")
    if risk_high:
        parts.append("risk")
    if not parts:
        return None
    return "|".join(parts)


def should_show(signature: str | None, dismissed: str | None) -> bool:
    """One banner per distinct signature. Dismiss hides that signature only."""
    return bool(signature) and signature != dismissed


def margin_is_low(margin_level: float | None, warn_level: float = MARGIN_WARN_LEVEL) -> bool:
    if margin_level is None:
        return False
    try:
        level = float(margin_level)
    except (TypeError, ValueError):
        return False
    return math.isfinite(level) and level < warn_level


def risk_exceeds_cap(open_risk: float | None, equity: float | None, cap_pct: float) -> bool:
    if open_risk is None:
        return False
    try:
        risk = float(open_risk)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(risk):
        return True
    try:
        base = float(equity) if equity is not None else 0.0
    except (TypeError, ValueError):
        base = 0.0
    if not math.isfinite(base) or base <= 0:
        return risk > 0
    return risk > base * cap_pct / 100.0


def banner_lines(
    tickets: list[int],
    *,
    margin_low: bool,
    margin_level: float | None,
    warn_level: float,
    risk_high: bool,
    cap_pct: float,
) -> list[str]:
    lines: list[str] = []
    if tickets:
        listed = ", ".join(f"#{ticket}" for ticket in sorted(set(tickets)))
        lines.append(f"Positions with SL=0: {listed}")
    if margin_low:
        shown = f"{margin_level:.1f}%" if margin_level is not None else "—"
        lines.append(f"Margin level {shown} is below the warn level {warn_level:.1f}%")
    if risk_high:
        lines.append(f"Open risk exceeds the configured cap of {cap_pct:g}% of equity")
    return lines
