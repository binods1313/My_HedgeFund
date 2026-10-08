"""Footer stamp: git short hash and this process start time."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_FILETIME_EPOCH = 116444736000000000


def git_short_hash(run=None) -> str:
    """Short HEAD hash. The word unknown when git cannot answer."""

    def default_run():
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
            check=False,
        )

    try:
        completed = (run or default_run)()
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if getattr(completed, "returncode", 1) != 0:
        return "unknown"
    text = (getattr(completed, "stdout", "") or "").strip()
    if not text or any(char.isspace() for char in text):
        return "unknown"
    return text


def _filetime_to_iso(ticks: int) -> str:
    if ticks < _FILETIME_EPOCH:
        return "unknown"
    seconds = (ticks - _FILETIME_EPOCH) / 10_000_000
    moment = datetime.fromtimestamp(seconds, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _windows_creation_ticks() -> int | None:
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    kernel = ctypes.windll.kernel32
    creation = FILETIME()
    exit_time = FILETIME()
    kernel_time = FILETIME()
    user_time = FILETIME()
    ok = kernel.GetProcessTimes(
        kernel.GetCurrentProcess(),
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    )
    if not ok:
        return None
    return (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)


def process_start_iso(reader=None) -> str:
    """UTC start of this process. The word unknown when the clock cannot be read."""
    if reader is not None:
        try:
            ticks = reader()
        except OSError:
            return "unknown"
        if ticks is None:
            return "unknown"
        return _filetime_to_iso(int(ticks))
    if os.name != "nt":
        return "unknown"
    try:
        ticks = _windows_creation_ticks()
    except OSError:
        return "unknown"
    if ticks is None:
        return "unknown"
    return _filetime_to_iso(ticks)


def stamp_text(git_hash: str | None = None, started: str | None = None) -> str:
    digest = git_short_hash() if git_hash is None else git_hash
    when = process_start_iso() if started is None else started
    return f"Build {digest} · process start {when}"
