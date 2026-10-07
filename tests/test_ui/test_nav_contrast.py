"""Nav text on every accent tint, active and inactive."""

from ui.theme import NAV_ACTIVE_MIX, TOKENS
from ui.theme_prefs import contrast_ratio, mix

ACCENTS = {
    "exchange": TOKENS["accent_cyan"],
    "portfolio": TOKENS["accent_green"],
    "signals": TOKENS["accent_violet"],
    "news": TOKENS["accent_amber"],
    "journal": TOKENS["accent_journal"],
    "research": TOKENS["accent_research"],
}


def test_nav_text_on_active_and_inactive_fills() -> None:
    inactive = mix(TOKENS["panel_glass"], TOKENS["bg_deep"], 0.88)
    assert contrast_ratio(TOKENS["text"], inactive) >= 4.5
    for name, accent in ACCENTS.items():
        active = mix(accent, TOKENS["panel"], NAV_ACTIVE_MIX)
        ratio = contrast_ratio(TOKENS["text"], active)
        assert ratio >= 4.5, f"{name} active {ratio:.2f}"


def test_journal_token_on_panel() -> None:
    assert contrast_ratio(TOKENS["accent_journal"], TOKENS["panel"]) >= 3.0
    assert contrast_ratio(TOKENS["accent_research"], TOKENS["panel"]) >= 3.0
    assert contrast_ratio(TOKENS["text"], TOKENS["panel"]) >= 4.5
