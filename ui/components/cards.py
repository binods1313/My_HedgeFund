"""Card containers and escaped HTML tables. Colours come from theme tokens."""

from __future__ import annotations

import html
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

DISPLAY_TZ = ZoneInfo("Asia/Kolkata")


def accent_card(accent: str, key: str):
    """Bordered container whose stable class is ``st-key-card-<accent>-<key>``."""
    safe_accent = accent if accent in {"cyan", "green", "amber", "violet", "red", "research"} else "cyan"
    return st.container(border=True, key=f"card-{safe_accent}-{key}")


def render_html_table(frame: pd.DataFrame, *, empty: str = "—") -> None:
    st.html(_html_table(frame, empty=empty))


def render_journal_table(frame: pd.DataFrame, *, now: datetime | None = None) -> None:
    st.html(_journal_table(frame, now=now))


def _html_table(frame: pd.DataFrame, *, empty: str) -> str:
    if frame is None or frame.empty:
        return f'<p class="hf-empty">{html.escape(empty)}</p>'
    headers = "".join(f"<th>{html.escape(str(column))}</th>" for column in frame.columns)
    body: list[str] = []
    for row in frame.itertuples(index=False):
        cells = "".join(f"<td>{html.escape(_cell(value))}</td>" for value in row)
        body.append(f"<tr>{cells}</tr>")
    return (
        '<div class="table-scroll"><table class="hf-table"><thead><tr>'
        + headers
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
    )


def _journal_table(frame: pd.DataFrame, *, now: datetime | None) -> str:
    if frame is None or frame.empty:
        return '<p class="hf-empty">Journal history unavailable · —</p>'
    clock = now or datetime.now(timezone.utc)
    headers = ("When", "Severity", "Source", "Message")
    head = "".join(f"<th>{html.escape(name)}</th>" for name in headers)
    rows: list[str] = []
    for record in frame.to_dict("records"):
        absolute = _absolute_time(record.get("ts"))
        relative = relative_time(record.get("ts"), clock)
        severity = str(record.get("severity") or "INFO").upper()
        badge = _severity_badge(severity)
        source = html.escape(str(record.get("source") or "—"))
        message = html.escape(str(record.get("message") or "—"))
        when = (
            f'<span class="hf-rel" title="{html.escape(absolute)}">{html.escape(relative)}</span>'
        )
        rows.append(f"<tr><td>{when}</td><td>{badge}</td><td>{source}</td><td>{message}</td></tr>")
    return (
        '<div class="table-scroll"><table class="hf-table"><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _severity_badge(severity: str) -> str:
    kind = {"INFO": "info", "WARN": "warn", "WARNING": "warn", "ERROR": "error"}.get(severity, "info")
    label = {"info": "INFO", "warn": "WARN", "error": "ERROR"}[kind]
    return f'<span class="sev-badge sev-{kind}">{label}</span>'


def _cell(value: object) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    if isinstance(value, float) and math.isinf(value):
        return "UNBOUNDED"
    return str(value)


def relative_time(value: object, now: datetime) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return "—"
    seconds = int((now - parsed).total_seconds())
    if seconds < 45:
        return "just now" if seconds >= 0 else "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _absolute_time(value: object) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return "—"
    return parsed.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S IST")


def _parse_time(value: object) -> datetime | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text or text == "—":
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
