from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

try:
    from defusedxml import ElementTree as ET
    HAS_DEFUSEDXML = True
except ImportError:  # pragma: no cover - fallback for minimal installs
    from xml.etree import ElementTree as ET
    HAS_DEFUSEDXML = False

try:
    import streamlit as st  # type: ignore
except Exception:  # pragma: no cover - streamlit optional at import time
    st = None  # type: ignore

DEFAULT_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "news_cache.json"
MAX_RETRIES = 2
HIGH_IMPACT_CHIP_HOURS = 24
MAX_BODY_BYTES = 2 * 1024 * 1024
# The feed has no offset field; the observed 12:30pm USD/CAD release aligns with UTC.
SOURCE_TIMEZONE = timezone.utc
USER_AGENT = "HedgeFundBinodCalendar/1.0 (+read-only economic calendar display)"
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsEvent:
    currency: str
    title: str
    when_utc: datetime | None
    impact: str
    forecast: str
    previous: str
    calendar_date: date | None = None


@dataclass(frozen=True)
class FetchResult:
    events: list[NewsEvent]
    fetched_at: datetime
    status: str
    warnings: list[str]
    cached: bool = False


def normalize_impact(value: str | None) -> str:
    cleaned = (value or "Unknown").strip().casefold()
    return {
        "high": "High",
        "medium": "Medium",
        "low": "Low",
        "holiday": "Holiday",
    }.get(cleaned, "Unknown")


def parse_calendar_date(value: str | None) -> date | None:
    text = (value or "").strip()
    if not text or text.casefold() in {"all day", "tentative"}:
        return None
    for date_format in ("%m-%d-%Y", "%Y-%m-%d", "%d-%m-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    return None


def parse_when(
    date_value: str | None,
    time_value: str | None,
    source_timezone=SOURCE_TIMEZONE,
) -> datetime | None:
    """Parse feed-local date/time into an aware UTC timestamp; unknown times stay None."""
    day = parse_calendar_date(date_value)
    time_text = re.sub(r"\s+", "", (time_value or "").strip())
    if day is None or not time_text or time_text.casefold() in {"allday", "tentative"}:
        return None
    for time_format in ("%I:%M%p", "%H:%M"):
        try:
            parsed_time = datetime.strptime(time_text.upper(), time_format).time()
            local_time = datetime.combine(day, parsed_time).replace(tzinfo=source_timezone)
            return local_time.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _is_html_payload(raw: bytes) -> bool:
    sample = raw.lstrip().lower()
    return sample.startswith(b"<!doctype html") or sample.startswith(b"<html")


def _safe_xml_payload(raw: bytes) -> tuple[bytes | None, str | None]:
    if HAS_DEFUSEDXML:
        return raw, None
    text = raw.decode("utf-8-sig", errors="replace")
    text = re.sub(
        r"<!DOCTYPE\s+[^>\[]*(?:\[[\s\S]*?\]\s*)?>",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"<!ENTITY\s+[^>]*>", "", text, flags=re.IGNORECASE)
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, flags=re.IGNORECASE):
        return None, "unsafe XML declaration rejected"
    return text.encode("utf-8"), None


def parse_calendar_xml(raw: bytes | str) -> tuple[list[NewsEvent], list[str]]:
    """Parse a ForexFactory weekly XML response; malformed input never escapes."""
    warnings: list[str] = []
    try:
        payload = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw or b"")
        if not payload:
            return [], ["empty feed payload"]
        if _is_html_payload(payload):
            return [], ["HTML page intercepted instead of calendar XML"]
        if len(payload) > MAX_BODY_BYTES:
            return [], ["calendar XML exceeds the 2 MB safety limit"]
        safe_payload, safety_warning = _safe_xml_payload(payload)
        if safe_payload is None:
            return [], [safety_warning or "unsafe XML payload rejected"]
        root = ET.fromstring(safe_payload)
    except Exception as exc:
        return [], [f"XML parse error: {exc}"]

    events: list[NewsEvent] = []
    try:
        nodes = root.iter("event")
        for index, node in enumerate(nodes):
            try:
                title = (node.findtext("title") or "").strip()
                currency = (node.findtext("country") or "").strip().upper()
                if not title or not currency:
                    warnings.append(f"event {index}: missing title/currency")
                    continue
                date_text = (node.findtext("date") or "").strip()
                time_text = (node.findtext("time") or "").strip()
                events.append(
                    NewsEvent(
                        currency=currency,
                        title=title,
                        when_utc=parse_when(date_text, time_text),
                        impact=normalize_impact(node.findtext("impact")),
                        forecast=(node.findtext("forecast") or "").strip(),
                        previous=(node.findtext("previous") or "").strip(),
                        calendar_date=parse_calendar_date(date_text),
                    )
                )
            except Exception as exc:
                warnings.append(f"event {index}: malformed event ignored ({exc})")
    except Exception as exc:
        warnings.append(f"event list could not be traversed ({exc})")
    return events, warnings


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _write_cache(result: FetchResult) -> None:
    payload = {
        "fetched_at": _as_utc(result.fetched_at).isoformat(),
        "status": result.status,
        "warnings": result.warnings,
        "events": [
            {
                "currency": event.currency,
                "title": event.title,
                "when_utc": event.when_utc.isoformat() if event.when_utc else None,
                "calendar_date": event.calendar_date.isoformat() if event.calendar_date else None,
                "impact": event.impact,
                "forecast": event.forecast,
                "previous": event.previous,
            }
            for event in result.events
        ],
    }
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = CACHE_PATH.with_suffix(CACHE_PATH.suffix + ".tmp")
        temporary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary_path.replace(CACHE_PATH)
    except OSError as exc:
        LOG.warning("could not write calendar cache: %s", exc)


def _read_cache() -> FetchResult | None:
    if not CACHE_PATH.exists():
        return None
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        events: list[NewsEvent] = []
        for item in payload.get("events", []):
            when_text = item.get("when_utc")
            events.append(
                NewsEvent(
                    currency=str(item.get("currency", "")),
                    title=str(item.get("title", "")),
                    when_utc=_as_utc(datetime.fromisoformat(when_text)) if when_text else None,
                    impact=normalize_impact(item.get("impact")),
                    forecast=str(item.get("forecast", "")),
                    previous=str(item.get("previous", "")),
                    calendar_date=parse_calendar_date(item.get("calendar_date")),
                )
            )
        fetched_at = _as_utc(datetime.fromisoformat(payload["fetched_at"]))
        return FetchResult(events, fetched_at, "cached", list(payload.get("warnings", [])), cached=True)
    except Exception as exc:
        LOG.warning("could not read calendar cache: %s", exc)
        return None


def _fallback_result(status: str, warnings: list[str]) -> FetchResult:
    cached = _read_cache()
    if cached is not None:
        cached.warnings[:0] = warnings
        return cached
    return FetchResult([], datetime.now(timezone.utc), status, warnings)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value).astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _pause_before_retry(attempt: int, response=None) -> None:
    backoff = (2 ** attempt) + random.uniform(0.0, 0.5)
    retry_after = _retry_after_seconds((response.headers or {}).get("Retry-After")) if response is not None else None
    time.sleep(min(max(backoff, retry_after or 0.0), 30.0))


def _fetch_uncached(url: str, retries: int = MAX_RETRIES) -> FetchResult:
    attempts = max(0, min(int(retries), MAX_RETRIES))
    warnings: list[str] = []
    for attempt in range(attempts + 1):
        response = None
        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/xml,text/xml,application/rss+xml",
                },
                timeout=(3.05, 10),
            )
        except requests.RequestException as exc:
            warnings.append(f"network error: {exc}")
            if attempt < attempts:
                _pause_before_retry(attempt)
                continue
            return _fallback_result("network-error", warnings)
        except Exception as exc:
            LOG.warning("unexpected calendar request error: %s", exc)
            return _fallback_result("network-error", ["Calendar request failed"])

        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code == 429 or 500 <= status_code <= 599:
            warnings.append(f"HTTP {status_code}")
            if attempt < attempts:
                _pause_before_retry(attempt, response)
                continue
            return _fallback_result(f"http-{status_code}", warnings)
        if status_code != 200:
            return _fallback_result(f"http-{status_code}", [f"HTTP {status_code}"])

        body = getattr(response, "content", b"") or b""
        if len(body) > MAX_BODY_BYTES:
            return _fallback_result("body-too-large", ["Calendar response exceeds 2 MB"])
        if _is_html_payload(body):
            return _fallback_result("html-page", ["HTML landing/challenge page returned instead of XML"])
        content_type = str((getattr(response, "headers", {}) or {}).get("Content-Type", "")).lower()
        if "xml" not in content_type:
            return _fallback_result("bad-content-type", [f"Unexpected content type: {content_type or 'missing'}"])

        events, parse_warnings = parse_calendar_xml(body)
        if any(
            warning.startswith(("XML parse error", "unsafe XML", "HTML page", "empty feed"))
            for warning in parse_warnings
        ):
            return _fallback_result("parse-error", parse_warnings)
        result = FetchResult(events, datetime.now(timezone.utc), "ok", parse_warnings)
        _write_cache(result)
        return result

    return _fallback_result("network-error", warnings or ["Calendar feed unavailable"])


if st is not None:
    @st.cache_data(ttl=300, show_spinner=False)
    def _cached_fetch(url: str, retries: int) -> FetchResult:
        return _fetch_uncached(url, retries)
else:  # Standard-Python use remains available in environments without Streamlit.
    def _cached_fetch(url: str, retries: int) -> FetchResult:
        return _fetch_uncached(url, retries)


def fetch_news(
    url: str = DEFAULT_FEED_URL,
    *,
    allow_cache: bool = True,
    retries: int = MAX_RETRIES,
) -> FetchResult:
    """Fetch the XML feed, returning typed cache/error results and never raising."""
    try:
        retry_count = max(0, min(int(retries), MAX_RETRIES))
        return _cached_fetch(url, retry_count) if allow_cache else _fetch_uncached(url, retry_count)
    except Exception as exc:
        LOG.warning("calendar fetch wrapper failed: %s", exc)
        return _fallback_result("fetch-error", ["Calendar feed unavailable"])


def next_upcoming_high_impact(
    events: list[NewsEvent],
    now: datetime | None = None,
) -> NewsEvent | None:
    current = _as_utc(now or datetime.now(timezone.utc))
    upcoming = [
        event for event in events
        if event.impact == "High" and event.when_utc is not None and _as_utc(event.when_utc) >= current
    ]
    return min(upcoming, key=lambda event: _as_utc(event.when_utc)) if upcoming else None


def currencies_for_symbol(symbol: str | None) -> list[str]:
    value = re.sub(r"[^A-Z]", "", (symbol or "").upper())
    pair = re.match(r"^([A-Z]{3})([A-Z]{3})", value)
    if pair:
        return list(dict.fromkeys(pair.groups()))
    if value.startswith("UK"):
        return ["GBP"]
    if value.startswith(("US", "BTC", "ETH")):
        return ["USD"]
    return []


def high_impact_within(
    events: list[NewsEvent],
    currencies: list[str],
    *,
    minutes: int = 30,
    now: datetime | None = None,
) -> NewsEvent | None:
    current = _as_utc(now or datetime.now(timezone.utc))
    currency_set = {value.upper() for value in currencies}
    limit = current.timestamp() + minutes * 60
    matches = [
        event for event in events
        if event.impact == "High"
        and event.currency.upper() in currency_set
        and event.when_utc is not None
        and current <= _as_utc(event.when_utc)
        and _as_utc(event.when_utc).timestamp() <= limit
    ]
    return min(matches, key=lambda event: _as_utc(event.when_utc)) if matches else None


def countdown_text(event: NewsEvent, now: datetime | None = None) -> str:
    current = _as_utc(now or datetime.now(timezone.utc))
    seconds = max(0, int((_as_utc(event.when_utc) - current).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{event.currency} {event.title} in {hours:02d}:{minutes:02d}:{seconds:02d}"


def header_chip_event(events: list[NewsEvent], now: datetime | None = None) -> NewsEvent | None:
    """Next High-impact event only when it falls inside the chip window."""
    current = _as_utc(now or datetime.now(timezone.utc))
    event = next_upcoming_high_impact(events, current)
    if event is None or event.when_utc is None:
        return None
    stamp = _as_utc(event.when_utc)
    if stamp > current + timedelta(hours=HIGH_IMPACT_CHIP_HOURS):
        return None
    return event


def header_chip_text(events: list[NewsEvent], now: datetime | None = None) -> str:
    """Header chip. The empty state is a sentence, so the row is never a blank gap."""
    event = header_chip_event(events, now)
    if event is None:
        return f"No high-impact event in the next {HIGH_IMPACT_CHIP_HOURS}h"
    return countdown_text(event, now)
