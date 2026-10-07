"""B5 — WCAG contrast-ratio tests for ui/theme.py tokens."""

from __future__ import annotations

import math

import pytest

from ui.theme import TOKENS


def _srgb_to_linear(channel: float) -> float:
    normalized = channel / 255.0
    if normalized <= 0.03928:
        return normalized / 12.92
    return ((normalized + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_color: str) -> float:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    r_lin = _srgb_to_linear(r)
    g_lin = _srgb_to_linear(g)
    b_lin = _srgb_to_linear(b)
    return 0.2126 * r_lin + 0.7152 * g_lin + 0.0722 * b_lin


def contrast_ratio(fg: str, bg: str) -> float:
    lum_fg = _relative_luminance(fg)
    lum_bg = _relative_luminance(bg)
    lighter = max(lum_fg, lum_bg)
    darker = min(lum_fg, lum_bg)
    return (lighter + 0.05) / (darker + 0.05)


FOREGROUNDS = {
    "text": TOKENS["text"],
    "text_muted": TOKENS["text_muted"],
    "gain": TOKENS["gain"],
    "loss": TOKENS["loss"],
    "warn": TOKENS["warn"],
    "accent": TOKENS["accent"],
    "disabled_button_text": TOKENS["disabled_text"],
}

BACKGROUNDS = {
    "bg": TOKENS["bg"],
    "panel": TOKENS["panel"],
    "panel_hover": TOKENS["panel_hover"],
}

NORMAL_TEXT_THRESHOLD = 4.5
LARGE_TEXT_THRESHOLD = 3.0


@pytest.mark.parametrize("fg_name,bg_name", [
    ("text", "bg"),
    ("text", "panel"),
    ("text", "panel_hover"),
    ("text_muted", "bg"),
    ("text_muted", "panel"),
    ("text_muted", "panel_hover"),
    ("gain", "bg"),
    ("gain", "panel"),
    ("loss", "bg"),
    ("loss", "panel"),
    ("warn", "bg"),
    ("warn", "panel"),
    ("accent", "bg"),
    ("accent", "panel"),
    ("disabled_button_text", "panel"),
])
def test_foreground_background_contrast_meets_threshold(fg_name: str, bg_name: str) -> None:
    fg = FOREGROUNDS[fg_name]
    bg = BACKGROUNDS[bg_name]
    ratio = contrast_ratio(fg, bg)
    assert ratio >= NORMAL_TEXT_THRESHOLD, (
        f"{fg_name} on {bg_name}: {ratio:.2f}:1 is below {NORMAL_TEXT_THRESHOLD}:1"
    )


def _hex_channels(hex_color: str) -> tuple[int, int, int]:
    cleaned = hex_color.lstrip("#")
    return tuple(int(cleaned[index : index + 2], 16) for index in (0, 2, 4))


def composite_hex(glass: str, stop: str, alpha: float) -> str:
    """Paint a translucent glass panel over one gradient stop."""
    glass_channels = _hex_channels(glass)
    stop_channels = _hex_channels(stop)
    mixed = [
        round(alpha * glass_channels[index] + (1.0 - alpha) * stop_channels[index])
        for index in range(3)
    ]
    return "#{:02X}{:02X}{:02X}".format(*mixed)


ACCENT_TEXT = (
    "accent_cyan",
    "accent_green",
    "accent_amber",
    "accent_violet",
    "accent_red",
)
GRADIENT_STOPS = ("bg_deep", "bg_high")


@pytest.mark.parametrize("accent_name", ACCENT_TEXT)
@pytest.mark.parametrize("stop_name", GRADIENT_STOPS)
def test_accent_text_on_glass_over_gradient_stops(accent_name: str, stop_name: str) -> None:
    alpha = float(TOKENS["panel_glass_alpha"])
    background = composite_hex(TOKENS["panel_glass"], TOKENS[stop_name], alpha)
    ratio = contrast_ratio(TOKENS[accent_name], background)
    assert ratio >= NORMAL_TEXT_THRESHOLD, (
        f"{accent_name} on glass/{stop_name} ({background}): {ratio:.2f}:1 "
        f"is below {NORMAL_TEXT_THRESHOLD}:1"
    )
