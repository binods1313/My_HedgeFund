"""A1: the journal table shows kind, detail, severity and collapsed repeats.

These tests drive the pure rendering helpers in ``ui.components.cards``; they
need no Streamlit runtime and no MT5 terminal.
"""

from datetime import datetime, timezone
import json

import pandas as pd

from ui.components import cards

NOW = datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc)


def _row(ts: str, severity: str, message: str, context: dict | None = None,
         category: str = "account", source: str = "engine") -> dict:
    return {
        "ts": ts,
        "category": category,
        "severity": severity,
        "source": source,
        "message": message,
        "context_json": json.dumps(context) if context is not None else None,
    }


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_mismatch_row_shows_kind_and_specific_detail():
    frame = _frame([_row(
        "2026-10-14T03:27:01+00:00", "CRITICAL", "Account mismatch",
         {"kind": "account_mismatch",
         "reason": "configured account 5057151780 is not the logged-in account 9876543210"})])
    html = cards._journal_table(frame, now=NOW)
    assert ">Account mismatch</td>" in html          # kind column
    assert "configured account" in html               # specific detail, not bare title
    assert "5057151780" not in html                   # configured login masked
    assert "****1780" in html                         # last four digits kept
    # Only account-sized numbers are masked; a short number is not a login.


def test_offline_row_is_labelled_terminal_offline_not_mismatch():
    frame = _frame([_row(
        "2026-10-14T03:27:01+00:00", "WARNING", "Terminal offline",
        {"kind": "terminal_offline", "reason": "terminal not responding"})])
    html = cards._journal_table(frame, now=NOW)
    assert ">Terminal offline</td>" in html
    assert "Account mismatch" not in html


def test_critical_severity_gets_critical_css_class():
    frame = _frame([_row("2026-10-14T03:27:01+00:00", "CRITICAL", "x")])
    html = cards._journal_table(frame, now=NOW)
    assert "sev-critical" in html


def test_archived_mismatch_without_kind_renders_critical_not_info():
    """The archived row has severity CRITICAL and no context.kind.

    The old badge map had no CRITICAL entry and fell through to INFO.
    The stored severity must be the rendered severity.
    """
    frame = _frame([_row(
        "2026-10-13T20:45:57+00:00",
        "CRITICAL",
        "Account mismatch",
        {
            "phase": "startup",
            "reason": "no account is logged in",
            "configured_login": None,
            "live_login": None,
        },
    )])
    html = cards._journal_table(frame, now=NOW)
    assert "sev-critical" in html
    assert ">CRITICAL</span>" in html
    assert "sev-info" not in html
    assert ">account</td>" in html
    assert "no account is logged in" in html


def test_unknown_severity_is_shown_literally_not_downgraded():
    frame = _frame([_row("2026-10-14T03:27:01+00:00", "PANIC", "x")])
    html = cards._journal_table(frame, now=NOW)
    assert "PANIC" in html
    assert "sev-info" not in html


def test_repeats_collapse_into_one_row_with_count_and_last_time():
    # The journal query returns newest first (ORDER BY id DESC); the collapse
    # relies on that order to keep the most recent time, so feed it that way.
    rows = [
        _row(f"2026-10-14T0{h}:00:00+00:00", "CRITICAL", "Account mismatch",
             {"kind": "account_mismatch", "reason": "no account is logged in"})
        for h in range(7, 0, -1)
    ]
    frame = _frame(rows)
    groups = cards._collapse_repeats(frame.to_dict("records"))
    assert len(groups) == 1
    assert groups[0]["count"] == 7
    html = cards._journal_table(frame, now=NOW)
    assert html.count("<tr>") == 2          # header row + one data row
    assert "repeated 7 times" in html
    # Newest occurrence (07:00 UTC) shown in display time (Asia/Kolkata, UTC+5:30).
    assert "last 12:30" in html             # newest occurrence, not the oldest


def test_distinct_reasons_are_not_merged():
    frame = _frame([
        _row("2026-10-14T01:00:00+00:00", "CRITICAL", "Account mismatch",
             {"kind": "account_mismatch", "reason": "reason A"}),
        _row("2026-10-14T02:00:00+00:00", "CRITICAL", "Account mismatch",
             {"kind": "account_mismatch", "reason": "reason B"}),
    ])
    assert len(cards._collapse_repeats(frame.to_dict("records"))) == 2


def test_group_shows_worst_severity_of_its_members():
    frame = _frame([
        _row("2026-10-14T01:00:00+00:00", "INFO", "m", {"reason": "same"}),
        _row("2026-10-14T02:00:00+00:00", "CRITICAL", "m", {"reason": "same"}),
        _row("2026-10-14T03:00:00+00:00", "INFO", "m", {"reason": "same"}),
    ])
    groups = cards._collapse_repeats(frame.to_dict("records"))
    assert groups[0]["severity"] == "CRITICAL"


def test_malformed_context_does_not_break_rendering():
    frame = _frame([{
        "ts": "2026-10-14T03:27:01+00:00", "category": "account",
        "severity": "CRITICAL", "source": "engine",
        "message": "Account mismatch", "context_json": "{not json",
    }])
    html = cards._journal_table(frame, now=NOW)
    assert "Account mismatch" in html


def test_empty_journal_shows_empty_message():
    assert "unavailable" in cards._journal_table(pd.DataFrame(), now=NOW)
