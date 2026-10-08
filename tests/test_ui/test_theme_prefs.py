from pathlib import Path

import pytest
from pydantic import ValidationError

from ui.theme import TOKENS
from ui.theme_prefs import (
    Palette,
    adjust_for_contrast,
    contrast_ratio,
    default_palette,
    guard_palette,
    load_palette,
    mix,
    reset_palette,
    save_palette,
    semantic_warning,
)


def test_palette_rejects_unknown_key_and_bad_hex() -> None:
    with pytest.raises(ValidationError):
        Palette.model_validate({**default_palette().model_dump(), "extra": 1})
    with pytest.raises(ValidationError):
        Palette.model_validate({**default_palette().model_dump(), "primary": "cyan"})


def test_contrast_auto_adjust_reaches_aa() -> None:
    # A light colour on a mid background fails. The guard darkens it until the tint passes.
    adjusted, note = adjust_for_contrast("#FFFFFF", ("#5A5A62",), text=TOKENS["text"])
    assert note is not None
    assert "Lightness moved" in note
    fill = mix(adjusted, "#5A5A62", 0.22)
    assert contrast_ratio(TOKENS["text"], fill) >= 4.5
    # Near-black on the dark panel used to pass, because only the tint was checked.
    kept, quiet = adjust_for_contrast("#050505", (TOKENS["bg"], TOKENS["panel"]), text=TOKENS["text"])
    assert quiet is not None
    assert "Contrast" in quiet and ":1" in quiet
    assert kept != "#050505"
    assert contrast_ratio(kept, TOKENS["bg"]) >= 3.0
    assert contrast_ratio(kept, TOKENS["panel"]) >= 3.0


def test_hue_guard_warns_when_gain_and_loss_are_close() -> None:
    assert semantic_warning("#FF0000", "#FF2020") is not None
    assert semantic_warning(TOKENS["gain"], TOKENS["loss"]) is None
    _palette, notes = guard_palette(
        default_palette().model_copy(update={"gain": "#FF0000", "loss": "#FF2020"}),
        (TOKENS["panel"],),
    )
    assert any("Gain and loss" in note for note in notes)


def test_load_rejects_bad_file_and_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "ui_prefs.json"
    target.write_text('{"version": 1, "extra": true}', encoding="utf-8")
    assert load_palette(target).preset == "Aurora Cyan"

    save_palette(default_palette(), target)
    assert load_palette(target).primary == default_palette().primary
    reset_palette(target)
    assert load_palette(target).preset == "Aurora Cyan"
