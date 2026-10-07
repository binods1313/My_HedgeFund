"""Closed-bar files shared with the chart. This module does not open a terminal."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "data" / "market_bars"


@dataclass(frozen=True)
class Bar:
    time: str
    open: float
    high: float
    low: float
    close: float
    spread: float | None = None


def _token(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return cleaned or "unknown"


def _number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def publish_closed_rows(
    symbol: str,
    timeframe: str,
    rows: list[dict],
    root: Path | None = None,
) -> Path | None:
    """Write closed bars only. The caller drops the forming bar before this."""
    payload = []
    for row in rows:
        prices = [_number(row.get(name)) for name in ("open", "high", "low", "close")]
        if any(item is None for item in prices):
            continue
        opened, high, low, close = prices
        assert opened is not None and high is not None and low is not None and close is not None
        spread = _number(row.get("spread"))
        payload.append(
            {
                "time": str(row.get("time", "")),
                "open": opened,
                "high": high,
                "low": low,
                "close": close,
                "spread": spread,
            }
        )
    if not payload:
        return None
    target = (root or DEFAULT_ROOT) / f"{_token(symbol)}__{_token(timeframe)}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"rows": payload}), encoding="utf-8")
    return target


def load_bars(symbol: str, timeframe: str, root: Path | None = None) -> list[Bar]:
    target = (root or DEFAULT_ROOT) / f"{_token(symbol)}__{_token(timeframe)}.json"
    if not target.is_file():
        return []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    bars: list[Bar] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        prices = [_number(row.get(name)) for name in ("open", "high", "low", "close")]
        if any(item is None for item in prices):
            continue
        opened, high, low, close = prices
        assert opened is not None and high is not None and low is not None and close is not None
        bars.append(
            Bar(
                str(row.get("time", "")),
                opened,
                high,
                low,
                close,
                _number(row.get("spread")),
            )
        )
    return bars
