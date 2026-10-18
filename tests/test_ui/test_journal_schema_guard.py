"""The UI must not migrate a journal that is older than the code."""
from __future__ import annotations

import sqlite3

import pytest

from engine.journal import (
    MIGRATION_1,
    MIGRATION_2,
    MIGRATION_3,
    SCHEMA_VERSION,
    Journal,
    SchemaTooOld,
    utc_now_iso,
)
from ui import data_access
from ui.components.emergency_stop import queue_emergency_stop
from ui.components.header import _scan_badge


def _apply(path, statements, version: int) -> None:
    connection = sqlite3.connect(path)
    try:
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, utc_now_iso()),
        )
        connection.commit()
    finally:
        connection.close()


def _schema2(path) -> None:
    _apply(path, list(MIGRATION_1) + list(MIGRATION_2), 2)


def _schema3(path) -> None:
    _apply(path, list(MIGRATION_1) + list(MIGRATION_2) + list(MIGRATION_3), 3)


def _version_and_tables(path) -> tuple[int, set[str]]:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        version = connection.execute(
            "SELECT MAX(version) FROM schema_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        return int(version), tables
    finally:
        connection.close()


def test_system_status_leaves_a_schema_2_file_unmigrated(tmp_path):
    path = tmp_path / "old.db"
    _schema2(path)
    before = _version_and_tables(path)

    status = data_access.system_status(path)

    after = _version_and_tables(path)
    assert before == after
    assert after[0] == 2
    assert "scan_symbols" not in after[1]
    assert status["schema_notice"] == "Journal schema older than code"
    assert status["scan_loop"] == "Journal schema older than code"
    assert status["schema_version"] == 2
    assert _scan_badge(status) == "Journal schema older than code"


def test_system_status_leaves_a_schema_3_file_read_only(tmp_path):
    path = tmp_path / "v3.db"
    _schema3(path)
    before = _version_and_tables(path)

    status = data_access.system_status(path)

    after = _version_and_tables(path)
    assert after == before
    assert after[0] == 3
    assert status["schema_notice"] == "Journal schema older than code"
    assert status["scan_loop"] == "Journal schema older than code"
    assert status["schema_version"] == 3
    assert _scan_badge(status) == "Journal schema older than code"


def test_journal_migrate_false_raises_and_does_not_create_scan_symbols(tmp_path):
    path = tmp_path / "old.db"
    _schema2(path)
    with pytest.raises(SchemaTooOld, match="Journal schema older than code"):
        Journal(path, migrate=False)
    version, tables = _version_and_tables(path)
    assert version == 2
    assert "scan_symbols" not in tables
    assert SCHEMA_VERSION == 4


def test_a_missing_file_is_created_at_the_current_schema(tmp_path):
    """A new file is not an upgrade of an older journal."""
    path = tmp_path / "new.db"
    journal = Journal(path, migrate=False)
    try:
        assert journal.current_version() == SCHEMA_VERSION
    finally:
        journal.close()
    version, tables = _version_and_tables(path)
    assert version == SCHEMA_VERSION
    assert "scan_symbols" in tables


def test_emergency_stop_queues_on_a_schema_2_file_without_migrating(tmp_path):
    """The commands table exists at schema 2. The halt writes a row and stops."""
    path = tmp_path / "old.db"
    _schema2(path)
    before = _version_and_tables(path)
    assert "commands" in before[1]

    row_id = queue_emergency_stop(path)

    after = _version_and_tables(path)
    assert after == before
    assert after[0] == 2
    assert "scan_symbols" not in after[1]
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            "SELECT id, command, status FROM commands"
        ).fetchone()
    finally:
        connection.close()
    assert row == (row_id, "EMERGENCY_STOP", "PENDING")


def test_a_current_journal_reports_scan_fields(tmp_path):
    path = tmp_path / "current.db"
    journal = Journal(path)
    try:
        journal.set_state("SCANNING", mode="PAPER")
        journal.execute(
            "INSERT INTO scan_results "
            "(symbols_json, ranked_json, ts, status, symbol_count) "
            "VALUES ('[]', '[]', ?, 'SCAN_EMPTY', 4)",
            ("2026-10-16T01:00:00+00:00",),
        )
    finally:
        journal.close()

    status = data_access.system_status(path)
    assert status["schema_notice"] is None
    assert status["scan_running"] is True
    assert status["last_cycle_ts"] == "2026-10-16T01:00:00+00:00"
    assert status["symbols_scanned"] == 4
    assert status["scan_loop"] == (
        "scan_running=True last_cycle_ts=2026-10-16T01:00:00+00:00 "
        "symbols_scanned=4"
    )
    assert status["execution_mode"] == "approve_first"
    badge = _scan_badge(status)
    assert "scan_running=True" in badge
    assert "Not built yet" not in badge
