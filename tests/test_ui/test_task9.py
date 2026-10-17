"""Task 9 UI checks: theme, news filter, shared snapshot, chip, ideas, marks, Enable AI."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from formatting import format_money
from ui.components.cards import _html_table
from ui.components.chart import pattern_mark_specs
from ui.components.ideas_watchlist import Idea, ideas_frame
from ui.components.news_tab import build_news_html, filter_events
from ui.data_access import AccountSnapshot
from ui.patterns import PatternHit
from ui.services.news import FetchResult, NewsEvent, header_chip_event, header_chip_text
from ui.theme import TOKENS
from ui.theme_prefs import (
    MAX_PREFS_BYTES,
    Palette,
    adjust_for_contrast,
    default_palette,
    load_palette,
    parse_imported_palette,
    save_palette,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone.utc)


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        connected=True,
        mode="PAPER",
        engine_state="IDLE",
        heartbeat_age_s=1.0,
        currency="EUR",
        balance=99999.25,
        equity=99986.69,
        margin_used=12.50,
        free_margin=99974.19,
        margin_level=800.0,
        open_pnl=-12.56,
        open_risk=1.25,
        fetched_at=NOW,
    )


def test_invalid_hex_and_low_contrast_are_explicit(tmp_path: Path) -> None:
    path = tmp_path / "ui_prefs.json"
    original = default_palette()
    save_palette(original, path)
    with pytest.raises(ValueError):
        parse_imported_palette('{"primary": "BADHEX"}')
    assert load_palette(path) == original

    injected = original.model_dump()
    injected["primary"] = "#FFFFFF; } body { background: url(1)"
    with pytest.raises(ValueError):
        Palette.model_validate(injected)
    unknown = original.model_dump()
    unknown["evil"] = "nope"
    with pytest.raises(ValueError):
        Palette.model_validate(unknown)

    huge = tmp_path / "huge.json"
    huge.write_bytes(b"{" + b"a" * (MAX_PREFS_BYTES + 8))
    assert load_palette(huge) == default_palette()
    with pytest.raises(ValueError):
        parse_imported_palette("{" + ("a" * (MAX_PREFS_BYTES + 8)))

    adjusted, note = adjust_for_contrast("#FFFFFF", ("#5A5A62",), text=TOKENS["text"])
    assert note is not None
    assert adjusted in note
    assert "Contrast" in note and ":1" in note
    assert adjusted != "#FFFFFF"


def test_low_contrast_note_shows_ratio_and_adjusted_colour() -> None:
    from streamlit.testing.v1 import AppTest

    script = """
import streamlit as st
from ui.theme import TOKENS
from ui.theme_prefs import adjust_for_contrast
from ui.components.theme_studio import _preview
_adjusted, note = adjust_for_contrast("#FFFFFF", ("#5A5A62",), text=TOKENS["text"])
st.session_state["_theme_notes"] = [note]
_preview()
"""
    app = AppTest.from_string(script)
    app.run(timeout=30)
    warnings = [item.value for item in app.warning]
    assert any("Contrast" in value and ":1" in value and "#" in value for value in warnings)


def test_invalid_hex_is_an_inline_error() -> None:
    from streamlit.testing.v1 import AppTest

    script = """
import streamlit as st
from ui.theme_prefs import default_palette
from ui.components.theme_studio import _write_keys, _role_fields
if not st.session_state.get("_theme_ready"):
    _write_keys(default_palette())
_role_fields()
"""
    app = AppTest.from_string(script)
    app.run(timeout=30)
    app.text_input("w_theme_primary_hex").set_value("BADHEX").run(timeout=30)
    messages = [item.value for item in app.error]
    assert any("Primary accent must be #RRGGBB" in message for message in messages)


def test_open_position_toggle_keeps_matching_currencies() -> None:
    events = [
        NewsEvent("EUR", "CPI", NOW, "High", "", ""),
        NewsEvent("JPY", "BoJ", NOW, "High", "", ""),
        NewsEvent("ALL", "Holiday", NOW, "Holiday", "", ""),
    ]
    kwargs = dict(currencies=set(), impacts=set(), query="", window="This week", show_past=True, now=NOW)
    affected = {event.currency for event in filter_events(events, open_symbols=["EURUSD"], **kwargs)}
    everything = {event.currency for event in filter_events(events, open_symbols=None, **kwargs)}
    assert affected == {"EUR", "ALL"}
    assert everything == {"EUR", "JPY", "ALL"}


def test_news_and_ideas_escape_script_tags() -> None:
    event = NewsEvent("USD", "<script>alert(1)</script>", NOW, "High", "", "")
    page = build_news_html([event], now=NOW)
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    frame = ideas_frame([
        Idea(source="<script>alert(1)</script>", symbol="EURUSD", direction="watch", note="ok"),
    ])
    table = _html_table(frame, empty="none")
    assert "<script>" not in table
    assert "&lt;script&gt;" in table


def test_both_views_render_the_same_cents() -> None:
    from streamlit.testing.v1 import AppTest

    equity = format_money(99986.69, "EUR")
    pnl = format_money(-12.56, "EUR")
    script = f"""
import datetime as dt
import pandas as pd
from ui.data_access import AccountSnapshot
from ui.components.snapshot import RunSnapshot
from ui.services.news import FetchResult
from ui.components.header import _draw_metrics
from ui.components.auxiliary_views import render_portfolio_view
now = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone.utc)
account = AccountSnapshot(
    connected=True, mode="PAPER", engine_state="IDLE", heartbeat_age_s=1.0,
    currency="EUR", balance=99999.25, equity=99986.69, margin_used=12.50,
    free_margin=99974.19, margin_level=800.0, open_pnl=-12.56, open_risk=1.25,
    fetched_at=now,
)
snapshot = RunSnapshot(account, pd.DataFrame(), pd.DataFrame(), FetchResult([], now, "ok", []), {{}})
_draw_metrics(snapshot)
render_portfolio_view(snapshot)
"""
    app = AppTest.from_string(script)
    app.run(timeout=30)
    assert not app.exception
    labels = [(item.label, item.value) for item in app.metric]
    equities = [value for label, value in labels if label in {"Floating equity", "Equity"}]
    pnls = [value for label, value in labels if label == "Open P&L"]
    assert equities == [equity, equity]
    assert pnls == [pnl, pnl]


def test_fomc_chip_window_and_empty_state() -> None:
    soon = NewsEvent("USD", "FOMC", NOW + dt.timedelta(hours=2), "High", "", "")
    later = NewsEvent("USD", "CPI", NOW + dt.timedelta(hours=48), "High", "", "")
    assert "FOMC" in header_chip_text([soon], NOW)
    assert header_chip_text([later], NOW) == "No high-impact event in the next 24h"
    assert header_chip_text([], NOW) == "No high-impact event in the next 24h"


def test_calm_chip_style_is_neutral_not_red() -> None:
    """The empty-state chip must not use the red alarm treatment.

    Red is for an actual high-impact event inside the window. The empty state
    gets the neutral `calm` variant, wired in the header and defined in the
    theme without the --loss colour.
    """
    soon = NewsEvent("USD", "FOMC", NOW + dt.timedelta(hours=2), "High", "", "")
    assert header_chip_event([soon], NOW) is not None
    assert header_chip_event([], NOW) is None
    header = (ROOT / "ui" / "components" / "header.py").read_text(encoding="utf-8")
    theme = (ROOT / "ui" / "theme.py").read_text(encoding="utf-8")
    assert 'header_chip_event(events) is None' in header
    assert 'news-countdown{chip_class}' in header
    calm = theme.split(".news-countdown.calm", 1)[1].split("}}", 1)[0]
    assert "--loss" not in calm
    assert "--muted" in calm


def test_pattern_marks_ignore_session_state() -> None:
    frame = pd.DataFrame({
        "time": ["t0", "t1"],
        "open": [1.0, 1.1],
        "high": [1.2, 1.3],
        "low": [0.9, 1.0],
        "close": [1.1, 1.2],
    })
    hit = PatternHit(index=1, name="Hammer", bias="bullish", sl=None, tp=None)
    state = {"symbol": "EURUSD", "timeframe": "1H", "marks": ["old"]}
    first = pattern_mark_specs(frame, [hit])
    state.clear()
    state.update({"symbol": "GBPUSD", "timeframe": "4H"})
    second = pattern_mark_specs(frame, [hit])
    assert first == second
    assert first[0]["name"] == "Hammer"
    assert first[0]["index"] == 1


def test_nav_markup_has_names_and_a_focus_ring() -> None:
    nav = (ROOT / "ui" / "components" / "nav.py").read_text(encoding="utf-8")
    theme = (ROOT / "ui" / "theme.py").read_text(encoding="utf-8")
    finviz = (ROOT / "ui" / "components" / "finviz_links.py").read_text(encoding="utf-8")
    assert "button:focus-visible" in theme
    assert "Current page" in nav
    assert "Facts" in nav
    assert "st.button(" in nav
    assert 'st.link_button("",' not in finviz
    assert 'st.link_button(label, url)' in finviz
    assert "<a></a>" not in nav
    assert 'aria-current="page">Current page</span>' in nav


def test_enable_ai_is_disabled_in_live_and_not_sent_by_rendering(tmp_path: Path) -> None:
    from streamlit.testing.v1 import AppTest

    from ui.components.enable_ai import enable_ai_availability, queue_enable_ai

    assert enable_ai_availability("LIVE") == (False, "Disabled in LIVE mode. Paper mode only.")
    assert enable_ai_availability("PAPER")[0] is True
    database = tmp_path / "engine.db"
    script = """
from ui.components.enable_ai import render_enable_ai
render_enable_ai("LIVE")
"""
    app = AppTest.from_string(script)
    app.run(timeout=30)
    assert app.button("btn_enable_ai").disabled is True
    captions = [item.value for item in app.caption]
    assert any("LIVE" in value for value in captions)
    assert not database.exists()

    queued = queue_enable_ai(database)
    assert queued > 0
    import sqlite3
    conn = sqlite3.connect(database)
    row = conn.execute("SELECT command, params_json FROM commands").fetchone()
    conn.close()
    assert row[0] == "ENABLE_AI"
    assert "submit_order" not in row[1]
    assert "close" not in row[1].lower()
