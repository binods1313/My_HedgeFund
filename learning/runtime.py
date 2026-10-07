"""Background shadow thread. It never calls Streamlit and never places an order."""

from __future__ import annotations

import threading
from pathlib import Path

from learning.shadow import ROOT, run_cycle

FLAG_PATH = ROOT / "data" / "shadow_mode.on"
_LOCK = threading.Lock()
_STOP = threading.Event()
_THREAD: threading.Thread | None = None


def sync_flag(enabled: bool, path: Path | None = None) -> None:
    target = path or FLAG_PATH
    if enabled:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("on", encoding="utf-8")
        return
    target.unlink(missing_ok=True)


def ensure_thread(enabled: bool) -> None:
    """Start the daemon only while the toggle is on. A second call does not stack threads."""
    global _THREAD
    if not enabled:
        return
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _STOP.clear()

        def _loop() -> None:
            while not _STOP.is_set():
                if FLAG_PATH.is_file():
                    try:
                        run_cycle(enabled=True)
                    except Exception:
                        pass
                _STOP.wait(30)

        _THREAD = threading.Thread(target=_loop, name="hf-shadow", daemon=True)
        _THREAD.start()


def stop_thread() -> None:
    _STOP.set()
