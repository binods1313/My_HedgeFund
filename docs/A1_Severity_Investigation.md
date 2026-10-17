# A1 severity investigation

Written 2026-10-16. Documentation of a UI rendering bug. No engine
change. PID 44436 was not restarted. The live Streamlit page was not
opened.

## Original issue

The journal stores an account mismatch at severity `CRITICAL`. The
dashboard badge mapped an unrecognised severity to INFO, so that row
rendered as a routine info badge.

The archived example has no `kind` field. A renderer that looks only at
`kind` would also fail this row. The defect that was fixed is the
severity fallback, not the missing kind.

## Evidence

Archive file, first line of
`backups/archive/account_mismatch_rows_20261014T044034Z.jsonl`:

```
id 10
ts 2026-10-13T20:45:57+00:00
severity CRITICAL
message Account mismatch
reason "no account is logged in"
category account
source engine
kind field: absent
```

This run's live outage is a different row and was not the A1 specimen.
Event 23630 is severity `WARNING`, kind `terminal_offline`. It should
not be read as the archived CRITICAL mismatch.

Case label used in the 2026-10-15 report: **B**. The stored severity was
`CRITICAL`, and the badge code before commit `7616d0f` had no CRITICAL
entry.

## Root cause

`ui/components/cards.py` `_severity_badge` looked up the severity in a
four-entry map. The default was `"info"`. The label map then printed
`INFO` for that kind. `CRITICAL` was not a key, so it took the default.

The journal write was already `CRITICAL`. The loss happened at render
time.

## Before

`HEAD` parent of `7616d0f` (`git show 7616d0f^:ui/components/cards.py`):

```python
def _severity_badge(severity: str) -> str:
    kind = {"INFO": "info", "WARN": "warn", "WARNING": "warn", "ERROR": "error"}.get(severity, "info")
    label = {"info": "INFO", "warn": "WARN", "error": "ERROR"}[kind]
    return f'<span class="sev-badge sev-{kind}">{label}</span>'
```

`CRITICAL` produced `<span class="sev-badge sev-info">INFO</span>`.

## After

Commit `7616d0f`, file `ui/components/cards.py`, as it stands in the
tree:

```python
_SEVERITIES: dict[str, tuple[str, str]] = {
    "INFO": ("info", "INFO"),
    "WARN": ("warn", "WARN"),
    "WARNING": ("warn", "WARNING"),
    "ERROR": ("error", "ERROR"),
    "CRITICAL": ("critical", "CRITICAL"),
}

def _severity_badge(severity: str) -> str:
    kind, label = _SEVERITIES.get(
        str(severity).strip().upper(), ("unknown", str(severity).strip() or "UNKNOWN")
    )
    return f'<span class="sev-badge sev-{kind}">{html.escape(label)}</span>'
```

`CRITICAL` produces a `sev-critical` badge whose label is `CRITICAL`.
An unknown severity renders as its own text with kind `unknown`. It is
not rewritten to INFO.

The same commit reads `context_json` so a row that does carry `kind`
and `reason` can show them, and the status card reads last trade and
today's trade count from the journal. Those are additional UI reads.
They are not the severity bug.

## Fix

| Item | Value |
|---|---|
| Commit | `7616d0f1be7cac1c04d315b21bdf6dda5df03685` |
| Subject | `ui: render the stored journal severity` |
| Files | `ui/components/cards.py`, `ui/components/header.py`, `ui/data_access.py`, `tests/test_ui/test_journal_render.py` |
| Diff stat | 4 files, +269 / −16 |

`f0af48b` updated other dashboard panels. It is not this fix.

## Tests

`tests/test_ui/test_journal_render.py`:

```
test_archived_mismatch_without_kind_renders_critical_not_info
```

Recorded result: passed. The assertion is that the archived mismatch,
which has severity CRITICAL and no kind, renders `sev-critical` and does
not render as INFO.

Broader recorded run the same session, `tests/test_engine` and
`tests/test_ui`: `1012 passed, 6 skipped` in 24.82s.
`tests/test_dependencies.py` was run as `93 passed, 2 deselected`.
`TestAppRunsEndToEnd` was deselected on purpose. That test executes
`app.py`, and the header constructs `Journal` on the configured live
path, which would migrate the live file to schema 3 under PID 44436.

Label: **TESTED-ONLY** for the HTML badge. The test does not open a
browser.

## Remaining validation

| Item | State |
|---|---|
| Unit render of the archived row | Passed. TESTED-ONLY. |
| Live Streamlit page on this machine | NOT VERIFIED. The page was not opened. |
| PID 44436 picking up `7616d0f` | Not applicable. The badge is UI code. The engine process does not render it. The UI processes that were already up (Streamlit PIDs noted on 2026-10-15 as 17752 and 16084, started 18:21:59) were not restarted, so a running server still has whatever module it imported at its own start. |
| Opening the live journal through `Journal` to refresh the page | Not done. Schema 3 migration must not run under the live writer. |

No further code change is in this package.
