"""A1: the UI and the engine must read and write the same journal file.

If they resolve different paths, the dashboard shows an empty or stale journal
while the engine writes somewhere else. These tests pin the single source of
truth (``engine/config.py`` -> ``resolve_db_path``) on both sides.
"""

from pathlib import Path

from engine.config import load_engine_config
from engine.journal import resolve_db_path
from ui import data_access

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_ui_default_journal_path_equals_engine_configured_path():
    engine_path = load_engine_config().db_path          # what the engine writes
    ui_path = data_access._configured_db_path()         # what the UI reads
    assert ui_path == engine_path


def test_resolved_path_is_absolute_and_inside_project_for_relative_config():
    resolved = resolve_db_path("data/engine_5057151780.db")
    assert resolved.is_absolute()
    assert resolved == (PROJECT_ROOT / "data" / "engine_5057151780.db").resolve()


def test_absolute_config_path_is_honoured_as_given(tmp_path):
    target = tmp_path / "custom.db"
    assert resolve_db_path(str(target)) == target


def test_system_status_reports_the_same_path_the_engine_uses():
    status = data_access.system_status()
    assert status["journal_path"] == str(load_engine_config().db_path)
