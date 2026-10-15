"""Card containers and escaped HTML tables. Colours come from theme tokens."""

from __future__ import annotations

import html
import json
import math
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

DISPLAY_TZ = ZoneInfo("Asia/Kolkata")

#: Account logins inside a detail string, for the masked rendering. Six digits
#: is the floor: shorter runs are far more likely to be a currency amount, a
#: spread or a bar count than an account number.
_LOGIN_RE = re.compile(r"\b\d{6,}\b")


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


#: Event kind code -> the words the operator reads. The engine writes the codes
#: (`context.kind`); a journal that renders the message instead of the kind
#: shows "Account mismatch" for a wrong account and "Account mismatch" for an
#: unreadable terminal, which are different incidents needing different fixes.
_KIND_LABELS: dict[str, str] = {
    "account_mismatch": "Account mismatch",
    "terminal_offline": "Terminal offline",
}

#: Severity -> (css kind, label). The journal writes INFO, WARNING and CRITICAL.
#: CRITICAL is the whole reason this map exists: the previous version had no
#: entry for it and fell through to INFO, so the guard's critical account
#: mismatch rendered as a routine blue note. An unrecognised severity now
#: renders as its literal self rather than being silently downgraded -- a
#: badge that guesses is worse than one that admits it does not know.
_SEVERITIES: dict[str, tuple[str, str]] = {
    "INFO": ("info", "INFO"),
    "WARN": ("warn", "WARN"),
    "WARNING": ("warn", "WARNING"),
    "ERROR": ("error", "ERROR"),
    "CRITICAL": ("critical", "CRITICAL"),
}

#: Ordering used only to pick the worst severity inside a collapsed repeat
#: group. A group is shown by its most severe member, so folding a CRITICAL
#: into a run of INFO rows can never hide it.
_SEVERITY_RANK: dict[str, int] = {
    "INFO": 0, "WARN": 1, "WARNING": 1, "ERROR": 2, "CRITICAL": 3,
}


def _journal_table(frame: pd.DataFrame, *, now: datetime | None) -> str:
    if frame is None or frame.empty:
        return '<p class="hf-empty">Journal history unavailable · —</p>'
    clock = now or datetime.now(timezone.utc)
    headers = ("When", "Severity", "Kind", "Detail", "Source")
    head = "".join(f"<th>{html.escape(name)}</th>" for name in headers)
    rows: list[str] = []
    for group in _collapse_repeats(frame.to_dict("records")):
        absolute = _absolute_time(group["ts"])
        relative = relative_time(group["ts"], clock)
        badge = _severity_badge(group["severity"])
        source = html.escape(str(group["source"] or "—"))
        kind = html.escape(group["kind"])
        detail = html.escape(group["detail"])
        when = (
            f'<span class="hf-rel" title="{html.escape(absolute)}">'
            f"{html.escape(relative)}</span>"
        )
        if group["count"] > 1:
            when += (
                f'<br><span class="hf-repeat">repeated {group["count"]} times, '
                f'last {html.escape(_clock_time(group["ts"]))}</span>'
            )
        rows.append(
            f"<tr><td>{when}</td><td>{badge}</td><td>{kind}</td>"
            f"<td>{detail}</td><td>{source}</td></tr>"
        )
    return (
        '<div class="table-scroll"><table class="hf-table"><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _collapse_repeats(records: list[dict]) -> list[dict]:
    """Fold identical events into one row, newest first.

    The engine's guard is throttled to one event per change plus a reminder
    every ten minutes, so a healthy journal has few repeats -- but a journal
    written by an engine that predates the throttle holds thousands of
    identical rows, and a reader that shows them one per line buries the
    transitions that matter. Grouping by (kind, detail) and reporting the
    count and the most recent occurrence keeps the incident and drops the
    noise. Records arrive newest first, so the first sighting is the "last".

    The clock time is the *most recent* occurrence, which is what an operator
    means by "last"; the oldest row in the group is not shown because the
    first event of an incident is already a row of its own in the window.
    """
    groups: dict[tuple[str, str], dict] = {}
    for record in records:
        kind, detail = _kind_and_detail(record)
        severity = str(record.get("severity") or "INFO").upper()
        seen = groups.get((kind, detail))
        if seen is None:
            groups[(kind, detail)] = {
                "ts": record.get("ts"),
                "count": 1,
                "severity": severity,
                "source": record.get("source"),
                "kind": kind,
                "detail": detail,
            }
            continue
        seen["count"] += 1
        if _SEVERITY_RANK.get(severity, 0) > _SEVERITY_RANK.get(seen["severity"], 0):
            seen["severity"] = severity
    return list(groups.values())


def _kind_and_detail(record: dict) -> tuple[str, str]:
    """The event's kind and its specific detail -- never a bare generic title."""
    context = _context(record)
    category = str(record.get("category") or "").strip()
    message = str(record.get("message") or "").strip()
    raw_kind = str(context.get("kind") or "").strip()
    kind = _KIND_LABELS.get(raw_kind) or raw_kind or category or "—"
    detail = str(context.get("reason") or "").strip() or message or "—"
    if category == "account":
        detail = _mask_logins(detail)
    return kind, detail


def _context(record: dict) -> dict:
    """Parse ``context_json``; a missing or malformed blob is simply empty."""
    raw = record.get("context_json")
    if raw is None or (isinstance(raw, float) and math.isnan(raw)):
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _mask_logins(text: str) -> str:
    """Mask account logins in an account-category detail string.

    The guard's detail names both accounts in full. Every other place the UI
    shows a login it shows the last four digits only, so the journal would be
    the one surface leaking them. Scoped to the ``account`` category on
    purpose: a detail carrying a price must not have its digits rewritten.
    """
    return _LOGIN_RE.sub(lambda match: "****" + match.group(0)[-4:], text)


def _clock_time(value: object) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return "—"
    return parsed.astimezone(DISPLAY_TZ).strftime("%H:%M")


def _severity_badge(severity: str) -> str:
    kind, label = _SEVERITIES.get(
        str(severity).strip().upper(), ("unknown", str(severity).strip() or "UNKNOWN")
    )
    return f'<span class="sev-badge sev-{kind}">{html.escape(label)}</span>'


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
