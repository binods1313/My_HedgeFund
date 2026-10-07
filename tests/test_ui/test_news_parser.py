from datetime import date, datetime, timezone
from types import SimpleNamespace

import ui.services.news as news
from ui.services.news import (
    FetchResult,
    NewsEvent,
    countdown_text,
    currencies_for_symbol,
    high_impact_within,
    next_upcoming_high_impact,
    parse_calendar_xml,
    parse_when,
)


def test_parse_production_weeklyevents_xml() -> None:
    raw = b'''
    <weeklyevents>
      <event>
        <title>Nonfarm Payrolls</title>
        <country>USD</country>
        <date><![CDATA[ 10-09-2026 ]]></date>
        <time><![CDATA[ 12:30pm ]]></time>
        <impact>High</impact>
        <forecast>200K</forecast>
        <previous>150K</previous>
        <url>https://www.forexfactory.com/calendar/example</url>
      </event>
    </weeklyevents>
    '''
    events, warnings = parse_calendar_xml(raw)
    assert len(events) == 1
    assert events[0].currency == "USD"
    assert events[0].impact == "High"
    assert events[0].calendar_date == date(2026, 10, 9)
    assert events[0].when_utc == datetime(2026, 10, 9, 12, 30, tzinfo=timezone.utc)
    assert events[0].forecast == "200K"
    assert not warnings


def test_parse_truncated_xml() -> None:
    raw = b'<weeklyevents><event><title>Broken'
    events, warnings = parse_calendar_xml(raw)
    assert events == []
    assert warnings


def test_parse_missing_fields() -> None:
    raw = b'''
    <weeklyevents>
      <event>
        <title>Only title</title>
      </event>
    </weeklyevents>
    '''
    events, warnings = parse_calendar_xml(raw)
    assert events == []
    assert warnings


def test_parse_all_day_and_tentative() -> None:
    raw = b'''
    <weeklyevents>
      <event><title>A</title><country>USD</country><date>10-05-2026</date><time>All Day</time><impact>Low</impact></event>
      <event><title>B</title><country>EUR</country><date>10-05-2026</date><time>Tentative</time><impact>Medium</impact></event>
      <event><title>C</title><country>JPY</country><date>10-05-2026</date><time>1:30pm</time><impact>Holiday</impact></event>
    </weeklyevents>
    '''
    events, warnings = parse_calendar_xml(raw)
    assert len(events) == 3
    assert all(isinstance(event, NewsEvent) for event in events)
    assert events[0].calendar_date == date(2026, 10, 5)
    assert events[0].when_utc is None
    assert events[1].when_utc is None
    assert events[2].when_utc == datetime(2026, 10, 5, 13, 30, tzinfo=timezone.utc)
    assert warnings == []


def test_html_landing_page_is_intercepted() -> None:
    events, warnings = parse_calendar_xml(b"<!doctype html><html><body>challenge</body></html>")
    assert events == []
    assert warnings == ["HTML page intercepted instead of calendar XML"]


def test_entity_declarations_are_rejected_without_expansion() -> None:
    raw = b'''<!DOCTYPE weeklyevents [<!ENTITY x "expanded">]>
    <weeklyevents><event><title>&x;</title><country>USD</country></event></weeklyevents>'''
    events, warnings = parse_calendar_xml(raw)
    assert events == []
    assert warnings


def test_unknown_time_is_preserved_as_unscheduled_event() -> None:
    raw = b'''<weeklyevents><event><title>Odd time</title><country>USD</country>
    <date>10-05-2026</date><time>later this week</time><impact>Unknownish</impact></event></weeklyevents>'''
    events, warnings = parse_calendar_xml(raw)
    assert len(events) == 1
    assert events[0].when_utc is None
    assert events[0].impact == "Unknown"
    assert warnings == []


def test_parse_iso_date_with_24_hour_time() -> None:
    assert parse_when("2026-10-09", "13:30") == datetime(
        2026, 10, 9, 13, 30, tzinfo=timezone.utc
    )


def test_fetch_retries_429_and_5xx_twice_then_parses(monkeypatch, tmp_path) -> None:
    body = b'''<weeklyevents><event><title>Claims</title><country>USD</country>
    <date>10-08-2026</date><time>12:30pm</time><impact>Medium</impact></event></weeklyevents>'''
    responses = [
        SimpleNamespace(status_code=429, headers={"Retry-After": "0"}, content=b"busy"),
        SimpleNamespace(status_code=503, headers={}, content=b"busy"),
        SimpleNamespace(status_code=200, headers={"Content-Type": "application/xml"}, content=body),
    ]
    calls = []
    sleeps = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(news.requests, "get", fake_get)
    monkeypatch.setattr(news.time, "sleep", sleeps.append)
    monkeypatch.setattr(news.random, "uniform", lambda _low, _high: 0)
    monkeypatch.setattr(news, "CACHE_PATH", tmp_path / "news_cache.json")

    result = news.fetch_news(allow_cache=False)

    assert result.status == "ok"
    assert len(result.events) == 1
    assert len(calls) == 3
    assert len(sleeps) == 2
    assert calls[-1][1]["timeout"] == (3.05, 10)
    assert calls[-1][1]["headers"]["User-Agent"] == news.USER_AGENT
    assert (tmp_path / "news_cache.json").exists()


def test_html_fetch_falls_back_to_disk_cache(monkeypatch, tmp_path) -> None:
    cache_path = tmp_path / "news_cache.json"
    monkeypatch.setattr(news, "CACHE_PATH", cache_path)
    cached_event = NewsEvent(
        currency="USD",
        title="Payrolls",
        when_utc=datetime(2026, 10, 9, 12, 30, tzinfo=timezone.utc),
        impact="High",
        forecast="200K",
        previous="150K",
        calendar_date=date(2026, 10, 9),
    )
    cached_at = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    news._write_cache(FetchResult([cached_event], cached_at, "ok", []))
    monkeypatch.setattr(
        news.requests,
        "get",
        lambda *_args, **_kwargs: SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "text/html"},
            content=b"<html>challenge</html>",
        ),
    )

    result = news.fetch_news(allow_cache=False)

    assert result.cached
    assert result.status == "cached"
    assert result.events[0].title == "Payrolls"
    assert result.fetched_at == cached_at
    assert any("HTML" in warning for warning in result.warnings)


def test_upcoming_high_impact_and_currency_pair_helpers() -> None:
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    event = NewsEvent(
        currency="USD",
        title="CPI",
        when_utc=datetime(2026, 10, 5, 12, 15, tzinfo=timezone.utc),
        impact="High",
        forecast="",
        previous="",
        calendar_date=date(2026, 10, 5),
    )
    later = NewsEvent(
        currency="EUR",
        title="Rate decision",
        when_utc=datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc),
        impact="High",
        forecast="",
        previous="",
        calendar_date=date(2026, 10, 5),
    )

    assert currencies_for_symbol("EURGBP#") == ["EUR", "GBP"]
    assert next_upcoming_high_impact([later, event], now) == event
    assert high_impact_within([event], ["USD"], minutes=30, now=now) == event
    assert high_impact_within([event], ["EUR"], minutes=30, now=now) is None
    assert countdown_text(event, now) == "USD CPI in 00:15:00"
