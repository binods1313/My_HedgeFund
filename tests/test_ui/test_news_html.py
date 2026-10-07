"""News HTML is escaped, and the default window hides past events."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ui.components.news_tab import build_news_html, filter_events
from ui.services.news import NewsEvent

NOW = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)


def _event(**overrides) -> NewsEvent:
    payload = {
        "currency": "USD",
        "title": "CPI",
        "when_utc": NOW + timedelta(hours=2),
        "impact": "High",
        "forecast": "0.3",
        "previous": "0.2",
    }
    payload.update(overrides)
    return NewsEvent(**payload)


def test_script_markup_in_a_title_is_escaped() -> None:
    html = build_news_html(
        [_event(title="<script>alert(1)</script>")],
        open_symbols=["EURUSD"],
        now=NOW,
    )
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_very_long_title_is_preserved() -> None:
    title = "Payrolls " + ("x" * 500)
    html = build_news_html([_event(title=title)], now=NOW)
    assert title in html


def test_missing_forecast_renders_a_dash() -> None:
    html = build_news_html([_event(forecast="")], open_symbols=[], now=NOW)
    assert "Forecast —" in html
    assert "Forecast None" not in html


def test_past_events_are_hidden_until_show_past() -> None:
    past = _event(title="Yesterday", when_utc=NOW - timedelta(days=2), impact="High")
    upcoming = _event(title="Later", impact="Medium")
    hidden = filter_events(
        [past, upcoming],
        currencies={"USD", "ALL"},
        impacts={"High", "Medium"},
        query="",
        window="This week",
        show_past=False,
        open_symbols=None,
        now=NOW,
    )
    shown = filter_events(
        [past, upcoming],
        currencies={"USD", "ALL"},
        impacts={"High", "Medium"},
        query="",
        window="This week",
        show_past=True,
        open_symbols=None,
        now=NOW,
    )
    assert [event.title for event in hidden] == ["Later"]
    assert {event.title for event in shown} == {"Yesterday", "Later"}
