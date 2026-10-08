"""Task 11 footer stamp. The caption is native Streamlit, not the hidden footer."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from ui.build_stamp import git_short_hash, process_start_iso, stamp_text


def test_stamp_degrades_when_git_is_unavailable() -> None:
    assert git_short_hash(run=lambda: (_ for _ in ()).throw(OSError("missing"))) == "unknown"
    failed = SimpleNamespace(returncode=1, stdout="")
    assert git_short_hash(run=lambda: failed) == "unknown"
    empty = SimpleNamespace(returncode=0, stdout="  \n")
    assert git_short_hash(run=lambda: empty) == "unknown"
    assert git_short_hash(run=lambda: (_ for _ in ()).throw(subprocess.TimeoutExpired("git", 5))) == "unknown"
    assert stamp_text(git_hash="unknown", started="2026-01-01T00:00:00Z") == (
        "Build unknown · process start 2026-01-01T00:00:00Z"
    )


def test_process_start_uses_the_injected_clock() -> None:
    # 2026-01-01T00:00:00Z as a Windows FILETIME (100 ns since 1601-01-01).
    ticks = 116444736000000000 + 1767225600 * 10_000_000
    assert process_start_iso(reader=lambda: ticks) == "2026-01-01T00:00:00Z"
    assert process_start_iso(reader=lambda: None) == "unknown"
    assert process_start_iso(reader=lambda: (_ for _ in ()).throw(OSError("clock"))) == "unknown"


def test_stamp_renders() -> None:
    from streamlit.testing.v1 import AppTest

    script = """
import streamlit as st
from ui.build_stamp import stamp_text
st.caption(stamp_text(git_hash="abc1234", started="2026-01-01T00:00:00Z"))
"""
    app = AppTest.from_string(script)
    app.run(timeout=30)
    assert not app.exception
    assert any(
        item.value == "Build abc1234 · process start 2026-01-01T00:00:00Z"
        for item in app.caption
    )
