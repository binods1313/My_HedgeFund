"""One donut centre size that still fits the narrowest column.

Plotly annotation sizes are pixels. They do not shrink when the browser
does, so the size is chosen for the 390px column and reused at 1280 and 1920.
"""

from __future__ import annotations

VIEWPORT_COLUMN_PX = {"1920": 560.0, "1280": 360.0, "390": 110.0}
HOLE = 0.62
CHART_PX = 190.0
MARGIN_PX = 8.0
EM = 0.62


def _line(label: str) -> str:
    return label.split("<", 1)[0]


def hole_px(column_px: float) -> float:
    diameter = min(max(column_px - MARGIN_PX, 1.0), CHART_PX - MARGIN_PX)
    return diameter * HOLE


def donut_center_font_size(label: str, column_px: float) -> int:
    chars = max(len(_line(label)), 1)
    raw = hole_px(column_px) / (chars * EM)
    return max(7, min(22, int(raw)))


def fitted_donut_font_size(label: str) -> int:
    return min(donut_center_font_size(label, width) for width in VIEWPORT_COLUMN_PX.values())


def text_px(label: str, size: int) -> float:
    return len(_line(label)) * size * EM


def fits_all_viewports(label: str, size: int | None = None) -> bool:
    chosen = fitted_donut_font_size(label) if size is None else size
    return all(text_px(label, chosen) <= hole_px(width) + 0.51 for width in VIEWPORT_COLUMN_PX.values())
