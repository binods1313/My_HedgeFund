"""Task 10 UI checks. Engine approvals stay off, so these tests do not touch orders."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ui.alerts import margin_notice_text
from ui.components.analytics_row import equity_figure, margin_figure, volume_figure, volume_status
from ui.components.chart import _build_figure, price_trace
from ui.equity_curve import equity_axis_range
from ui.performance import performance_report, slice_window
from ui.theme import TOKENS
from ui.theme_prefs import AA_MARK, adjust_for_contrast, contrast_ratio, default_palette, guard_palette

ROOT = Path(__file__).resolve().parents[2]
_BACKGROUNDS = (TOKENS["bg"], TOKENS["panel"], TOKENS["bg_deep"])


def _prices() -> pd.DataFrame:
    return pd.DataFrame({
        "time": ["t0", "t1", "t2"],
        "open": [1.10, 1.12, 1.11],
        "high": [1.14, 1.15, 1.13],
        "low": [1.08, 1.10, 1.09],
        "close": [1.12, 1.11, 1.125],
        "tick_volume": [10, 40, 15],
    })


def test_near_black_shows_the_ratio_and_an_adjusted_colour() -> None:
    adjusted, note = adjust_for_contrast("#101010", _BACKGROUNDS, text=TOKENS["text"])
    assert note is not None
    assert adjusted in note
    assert "Contrast" in note and ":1" in note
    assert adjusted != "#101010"
    for background in _BACKGROUNDS:
        assert contrast_ratio(adjusted, background) >= AA_MARK
    guarded, notes = guard_palette(default_palette(), _BACKGROUNDS)
    assert guarded.primary == default_palette().primary
    assert notes == []


def test_low_contrast_preview_mentions_the_adjusted_hex() -> None:
    from streamlit.testing.v1 import AppTest

    script = """
import streamlit as st
from ui.theme import TOKENS
from ui.theme_prefs import adjust_for_contrast
from ui.components.theme_studio import _preview
adjusted, note = adjust_for_contrast(
    "#101010", (TOKENS["bg"], TOKENS["panel"], TOKENS["bg_deep"]), text=TOKENS["text"]
)
st.session_state["_theme_notes"] = [note]
st.session_state["_adjusted"] = adjusted
_preview()
"""
    app = AppTest.from_string(script)
    app.run(timeout=30)
    warnings = [item.value for item in app.warning]
    assert any("Contrast" in value and ":1" in value and "#" in value for value in warnings)
    assert app.session_state["_adjusted"] != "#101010"


def test_commit_shows_the_hex_error_and_reset_export_exist() -> None:
    from streamlit.testing.v1 import AppTest

    script = """
import streamlit as st
from ui.theme_prefs import default_palette
from ui.components.theme_studio import _write_keys, _commit_draft, _file_row
if not st.session_state.get("_theme_ready"):
    _write_keys(default_palette())
st.session_state["w_theme_primary"] = "BADHEX"
st.session_state["w_theme_primary_hex"] = "BADHEX"
_commit_draft()
_file_row()
"""
    app = AppTest.from_string(script)
    app.run(timeout=30)
    messages = [item.value for item in app.error]
    assert any("not #RRGGBB" in message for message in messages)
    labels = [item.label for item in app.button]
    labels += [item.label for item in getattr(app, "download_button", [])]
    assert "Reset to default" in labels
    assert "Export JSON" in labels


def test_chart_styles_use_candlestick_scatter_and_ohlc() -> None:
    import streamlit as st

    frame = _prices()
    candle = price_trace("Candlestick", frame)
    area = price_trace("Area", frame)
    bars = price_trace("Bar", frame)
    assert candle.type == "candlestick"
    assert area.type == "scatter"
    assert bars.type == "ohlc"
    assert float(bars.open[0]) == 1.10
    assert float(bars.close[-1]) == 1.125
    pack = {"series": {}, "signals": {}}
    for style, expected in (("Candlestick", "candlestick"), ("Area", "scatter"), ("Bar", "ohlc")):
        st.session_state["chart_style"] = style
        figure = _build_figure(frame, "EURUSD", [], pack)
        assert figure.data[0].type == expected


def test_card_views_hover_and_name_the_tone() -> None:
    frame = _prices()
    bar = volume_figure(frame, "Bar")
    line = volume_figure(frame, "Line")
    assert bar.data[0].type == "bar"
    assert line.data[0].type == "scatter"
    assert "Tick volume" in bar.data[0].hovertemplate
    assert getattr(bar.data[0], "text", None) in (None, ())
    tone, sentence = volume_status(frame)
    assert tone == "safe" and sentence.startswith("safe")
    empty_tone, empty_sentence = volume_status(pd.DataFrame())
    assert empty_tone == "caution" and "caution" in empty_sentence
    parts = {"used": 20.0, "free": 80.0, "free_rest": 70.0, "buffer": 10.0, "total": 100.0}
    donut = margin_figure(parts, "Donut", None)
    columns = margin_figure(parts, "Bar", None)
    assert donut.data[0].type == "pie"
    assert donut.data[0].textinfo == "none"
    assert columns.data[0].type == "bar"
    rising = [(0.0, 100.0), (1.0, 101.0)]
    equity = equity_figure(rising, "Line")
    assert equity.data[-1].type == "scatter"
    assert "Equity" in equity.data[-1].hovertemplate


def test_equity_axis_keeps_a_minimum_span() -> None:
    low, high = equity_axis_range(100_000.0, 100_001.0)
    assert high - low >= 100_000.0 * 0.0005
    assert low < 100_000.0 and high > 100_001.0
    wide_low, wide_high = equity_axis_range(100.0, 200.0)
    assert wide_low < 100.0 and wide_high > 200.0


def test_performance_uses_equity_snapshots_and_hides_win_loss() -> None:
    short = [100.0 + index for index in range(10)]
    report = performance_report(slice_window(short, "All"), realised=None)
    assert report["label"] == "from equity snapshots"
    assert "sqrt(252)" in str(report["annualisation"])
    assert report["sharpe"] is None
    assert "win_loss" not in report
    assert report["max_drawdown_pct"] == 0.0
    change = report["change"]
    assert change is not None and change[0] == 9.0
    long = [100.0] * 20 + [100.0 + index * 0.2 for index in range(15)]
    assert len(long) >= 30
    assert performance_report(long)["sharpe"] is not None
    with_pnl = performance_report([100.0, 101.0], realised=[2.0, -1.0])
    assert with_pnl["win_loss"] == 1.0
    assert slice_window(list(range(40)), "Last 7") == list(range(33, 40))


def test_margin_notice_is_text_and_silent_at_the_warn_level() -> None:
    from streamlit.testing.v1 import AppTest

    assert margin_notice_text(99.0, 100.0) == "Margin level 99.0% is below the warn level 100.0%."
    assert margin_notice_text(100.0, 100.0) is None
    assert margin_notice_text(None, 100.0) is None
    banner = (ROOT / "ui" / "components" / "alert_banner.py").read_text(encoding="utf-8")
    assert "render_margin_notice" in banner
    assert "<audio" not in banner
    assert "Dismiss margin notice" in banner
    script = """
import streamlit as st
from ui.components.alert_banner import render_margin_notice
level = st.session_state["_level"] if "_level" in st.session_state else None
render_margin_notice(level, 100.0)
"""
    shown = AppTest.from_string(script)
    shown.session_state["_level"] = 99.0
    shown.run(timeout=30)
    assert any("below the warn level" in item.value for item in shown.warning)
    shown.button("btn_dismiss_margin_notice").click().run(timeout=30)
    shown.run(timeout=30)
    assert shown.warning == []
    assert shown.session_state["_margin_notice_dismissed"] == "margin:100.0"
    for level in (100.0, None):
        quiet = AppTest.from_string(script)
        if level is not None:
            quiet.session_state["_level"] = level
        quiet.run(timeout=30)
        assert quiet.warning == []
        assert quiet.button == []


def test_narrow_layout_and_chip_slot_are_in_the_markup() -> None:
    theme = (ROOT / "ui" / "theme.py").read_text(encoding="utf-8")
    header = (ROOT / "ui" / "components" / "header.py").read_text(encoding="utf-8")
    assert "max-width: 420px" in theme
    assert "grid-template-columns: 1fr 1fr" in theme
    assert "white-space: normal" in theme
    assert "prefers-reduced-motion: reduce" in theme
    assert "animation-direction: reverse" in theme
    assert "crystal-spin" in theme
    assert "hf-tone-caution" in theme
    assert 'class="news-countdown"' in header
    assert "header_chip_text" in header
