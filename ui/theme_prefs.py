"""Palette schema, contrast guard, and local preference file.

Hex values for presets live in ui.theme. This module only checks and stores them.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ui.theme import NAV_ACTIVE_MIX, PRESETS, TOKENS

HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
MAX_PREFS_BYTES = 16_384
HUE_WARN_DEGREES = 40.0
AA_TEXT = 4.5
AA_MARK = 3.0
DEFAULT_PREFS_PATH = Path(__file__).resolve().parents[1] / "data" / "ui_prefs.json"

_ROLES = (
    "primary",
    "accent_cyan",
    "accent_green",
    "accent_amber",
    "accent_violet",
    "accent_red",
    "accent_journal",
    "accent_research",
    "gain",
    "loss",
)


def _hex(value: str) -> str:
    if not isinstance(value, str) or HEX_RE.fullmatch(value) is None:
        raise ValueError("colour must be #RRGGBB")
    return value.upper()


class Palette(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    preset: str = "Aurora Cyan"
    primary: str
    accent_cyan: str
    accent_green: str
    accent_amber: str
    accent_violet: str
    accent_red: str
    accent_journal: str
    accent_research: str = "#A9C1FF"
    gain: str
    loss: str
    glow: str = Field(default="normal", pattern="^(off|low|normal)$")
    motion: str = Field(default="normal", pattern="^(off|low|normal)$")

    @field_validator(*_ROLES)
    @classmethod
    def _colours(cls, value: str) -> str:
        return _hex(value)


def default_palette() -> Palette:
    return Palette.model_validate(PRESETS["Aurora Cyan"])


def preset_palette(name: str) -> Palette:
    if name not in PRESETS:
        raise KeyError(name)
    return Palette.model_validate(PRESETS[name])


def _channel(hex_color: str) -> tuple[int, int, int]:
    text = hex_color.lstrip("#")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _linear(channel: float) -> float:
    normalized = channel / 255.0
    if normalized <= 0.04045:
        return normalized / 12.92
    return ((normalized + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    red, green, blue = _channel(hex_color)
    return 0.2126 * _linear(red) + 0.7152 * _linear(green) + 0.0722 * _linear(blue)


def contrast_ratio(foreground: str, background: str) -> float:
    left = relative_luminance(foreground)
    right = relative_luminance(background)
    lighter, darker = max(left, right), min(left, right)
    return (lighter + 0.05) / (darker + 0.05)


def mix(foreground: str, background: str, weight: float) -> str:
    """sRGB mix. ``weight`` is the share of ``foreground``."""
    left = _channel(foreground)
    right = _channel(background)
    parts = [round(weight * item + (1.0 - weight) * other) for item, other in zip(left, right)]
    return "#" + "".join(f"{max(0, min(255, part)):02X}" for part in parts)


def _hsl(hex_color: str) -> tuple[float, float, float]:
    red, green, blue = (channel / 255.0 for channel in _channel(hex_color))
    high, low = max(red, green, blue), min(red, green, blue)
    light = (high + low) / 2.0
    delta = high - low
    if delta == 0:
        return 0.0, 0.0, light
    saturation = delta / (1 - abs(2 * light - 1))
    if high == red:
        hue = ((green - blue) / delta) % 6
    elif high == green:
        hue = (blue - red) / delta + 2
    else:
        hue = (red - green) / delta + 4
    return hue * 60.0, saturation, light


def _from_hsl(hue: float, saturation: float, light: float) -> str:
    chroma = (1 - abs(2 * light - 1)) * saturation
    hp = (hue % 360) / 60.0
    match = light - chroma / 2
    sector = hp % 2 - 1
    x = chroma * (1 - abs(sector))
    if 0 <= hp < 1:
        red, green, blue = chroma, x, 0.0
    elif 1 <= hp < 2:
        red, green, blue = x, chroma, 0.0
    elif 2 <= hp < 3:
        red, green, blue = 0.0, chroma, x
    elif 3 <= hp < 4:
        red, green, blue = 0.0, x, chroma
    elif 4 <= hp < 5:
        red, green, blue = x, 0.0, chroma
    else:
        red, green, blue = chroma, 0.0, x
    parts = [round((channel + match) * 255) for channel in (red, green, blue)]
    return "#" + "".join(f"{max(0, min(255, part)):02X}" for part in parts)


def hue_distance(left: str, right: str) -> float:
    a, b = _hsl(left)[0], _hsl(right)[0]
    gap = abs(a - b) % 360.0
    return min(gap, 360.0 - gap)


def semantic_warning(gain: str, loss: str) -> str | None:
    distance = hue_distance(gain, loss)
    if distance < HUE_WARN_DEGREES:
        return (
            f"Gain and loss hues are {distance:.0f}° apart. "
            "Keep them further apart. ▲ and ▼ still mark direction."
        )
    return None


def _tint_passes(colour: str, backgrounds: tuple[str, ...], text: str) -> bool:
    for background in backgrounds:
        fill = mix(colour, background, NAV_ACTIVE_MIX)
        if contrast_ratio(text, fill) < AA_TEXT:
            return False
    return True


def _mark_passes(colour: str, backgrounds: tuple[str, ...]) -> bool:
    """The colour itself must stay visible on every background, not only as a tint."""
    return all(contrast_ratio(colour, background) >= AA_MARK for background in backgrounds)


def _mark_ratio(colour: str, backgrounds: tuple[str, ...]) -> float:
    if not backgrounds:
        return 0.0
    return min(contrast_ratio(colour, background) for background in backgrounds)


def adjust_for_contrast(
    colour: str, backgrounds: tuple[str, ...], *, text: str | None = None
) -> tuple[str, str | None]:
    """Move lightness until the tint keeps text at AA and the colour clears the background.

    A near-black accent on a dark panel used to pass: page text on a 22% tint
    stayed readable, while the accent itself disappeared into the panel.
    """
    ink = text or TOKENS["text"]
    hue, saturation, light = _hsl(colour)
    if _tint_passes(colour, backgrounds, ink) and _mark_passes(colour, backgrounds):
        return colour.upper(), None
    best = colour.upper()
    best_gap = math.inf
    best_ratio = 0.0
    for step in range(0, 101):
        candidate_light = step / 100.0
        candidate = _from_hsl(hue, saturation, candidate_light)
        if not _tint_passes(candidate, backgrounds, ink):
            continue
        if not _mark_passes(candidate, backgrounds):
            continue
        gap = abs(candidate_light - light)
        if gap < best_gap:
            best_gap = gap
            best = candidate
            best_ratio = _mark_ratio(candidate, backgrounds)
    if best_gap is math.inf:
        return colour.upper(), "No lightness keeps this hue readable. The previous colour stays."
    return best, (
        f"Adjusted to {best}. Contrast {best_ratio:.2f}:1. "
        f"Lightness moved from {light:.2f} to {_hsl(best)[2]:.2f}."
    )


def guard_palette(palette: Palette, backgrounds: tuple[str, ...]) -> tuple[Palette, list[str]]:
    notes: list[str] = []
    data = palette.model_dump()
    for role in _ROLES:
        adjusted, note = adjust_for_contrast(data[role], backgrounds)
        if note:
            notes.append(f"{role}: {note}")
        data[role] = adjusted
    warning = semantic_warning(data["gain"], data["loss"])
    if warning:
        notes.append(warning)
    return Palette.model_validate(data), notes


def load_palette(path: Path | None = None) -> Palette:
    target = path or DEFAULT_PREFS_PATH
    if not target.exists():
        return default_palette()
    try:
        if target.stat().st_size > MAX_PREFS_BYTES:
            return default_palette()
        payload = json.loads(target.read_text(encoding="utf-8"))
        return Palette.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError, ValueError):
        return default_palette()


def parse_imported_palette(raw: str) -> Palette:
    """Reject oversized JSON, unknown keys, and any colour that is not #RRGGBB."""
    if len(raw.encode("utf-8")) > MAX_PREFS_BYTES:
        raise ValueError("import is too large")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("import must be an object")
    return Palette.model_validate(payload)


def save_palette(palette: Palette, path: Path | None = None) -> None:
    target = path or DEFAULT_PREFS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(palette.model_dump_json(indent=2), encoding="utf-8")


def reset_palette(path: Path | None = None) -> Palette:
    palette = default_palette()
    save_palette(palette, path)
    return palette
