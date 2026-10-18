from __future__ import annotations

import sqlite3

import pytest

from engine import journal as journal_module
from engine.journal import (
    MIGRATIONS,
    SCHEMA_VERSION,
    Journal,
    backup_journal,
    utc_now_iso,
)


def _create_v3(path, rows=()):
    connection = sqlite3.connect(path)
    try:
        for version in (1, 2, 3):
            for statement in MIGRATIONS[version]:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, utc_now_iso()),
            )
        connection.executemany(
            "INSERT INTO signals (symbol, direction, score, bar_ts, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def _read(path, sql, params=()):
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def test_v4_migration_rebuilds_signals_and_preserves_legacy_rows(tmp_path):
    path = tmp_path / "v3.db"
    _create_v3(path, [
        ("EURUSD", "BUY", 0.75, 100, "2026-10-17T10:00:00+00:00"),
        ("GBPUSD", "SELL", 0.50, 200, "2026-10-17T11:00:00+00:00"),
    ])

    journal = Journal(path)
    try:
        assert journal.current_version() == 4
        rows = journal.query(
            "SELECT id, symbol, direction, score, bar_ts, ts, decision_id, "
            "status, strategy, timeframe FROM signals ORDER BY id")
        assert [tuple(row) for row in rows] == [
            (1, "EURUSD", "BUY", 0.75, 100,
             "2026-10-17T10:00:00+00:00", "LEGACY-1", "LEGACY",
             "legacy", "UNKNOWN"),
            (2, "GBPUSD", "SELL", 0.5, 200,
             "2026-10-17T11:00:00+00:00", "LEGACY-2", "LEGACY",
             "legacy", "UNKNOWN"),
        ]
        definitions = {
            row["name"]: row["sql"]
            for row in journal.query(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='index' AND tbl_name='signals' AND sql IS NOT NULL")
        }
        assert "idx_signals_bar_strategy" in definitions
        assert "WHERE is_test = 0 AND bar_ts IS NOT NULL" in (
            definitions["idx_signals_bar_strategy"])
        assert "idx_signals_one_pending" in definitions
        assert "WHERE status = 'PENDING'" in definitions["idx_signals_one_pending"]

        with pytest.raises(sqlite3.IntegrityError):
            journal.execute(
                "INSERT INTO signals "
                "(symbol, direction, score, bar_ts, ts, decision_id) "
                "VALUES ('TESTNULL', 'BUY', 1, NULL, ?, 'bad-null')",
                (utc_now_iso(),),
            )

        journal.execute(
            "INSERT INTO signals "
            "(symbol, direction, score, bar_ts, ts, decision_id, status, "
            "expires_at) VALUES ('EURUSD', 'BUY', 1, 300, ?, 'pending-1', "
            "'PENDING', ?)",
            (utc_now_iso(), utc_now_iso()),
        )
        with pytest.raises(sqlite3.IntegrityError):
            journal.execute(
                "INSERT INTO signals "
                "(symbol, direction, score, bar_ts, ts, decision_id, status, "
                "expires_at) VALUES ('EURUSD', 'SELL', 1, 400, ?, 'pending-2', "
                "'PENDING', ?)",
                (utc_now_iso(), utc_now_iso()),
            )
        journal.execute(
            "INSERT INTO signals "
            "(symbol, direction, score, bar_ts, ts, decision_id, is_test, "
            "test_tag) VALUES ('TEST', 'BUY', 1, NULL, ?, 'test-null', 1, "
            "'TEST SIGNAL')",
            (utc_now_iso(),),
        )
        assert journal.query_one(
            "SELECT decision_id FROM signals WHERE decision_id='test-null'")[
                "decision_id"] == "test-null"
    finally:
        journal.close()


def test_v4_migration_is_idempotent(tmp_path):
    path = tmp_path / "v3.db"
    _create_v3(path, [
        ("EURUSD", "BUY", 0.75, 100, "2026-10-17T10:00:00+00:00"),
    ])

    journal = Journal(path)
    try:
        before = [tuple(row) for row in journal.query(
            "SELECT id, decision_id, status FROM signals")]
        assert journal.migrate() == SCHEMA_VERSION == 4
        after = [tuple(row) for row in journal.query(
            "SELECT id, decision_id, status FROM signals")]
        assert after == before
        assert journal.query_one(
            "SELECT COUNT(*) AS n FROM schema_version WHERE version=4")["n"] == 1
    finally:
        journal.close()


def test_backup_journal_checkpoints_and_copies_wal_consistently(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    connection = sqlite3.connect(source)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE records (value TEXT NOT NULL)")
        connection.execute("INSERT INTO records VALUES ('committed-in-wal')")
        connection.commit()

        assert backup_journal(source, destination) == destination.resolve()
        assert _read(destination, "SELECT value FROM records") == [
            ("committed-in-wal",)]
        assert _read(destination, "PRAGMA integrity_check") == [("ok",)]
        with pytest.raises(FileExistsError):
            backup_journal(source, destination)
    finally:
        connection.close()


def test_interrupted_v4_migration_rolls_back_and_backup_is_recoverable(
        tmp_path, monkeypatch):
    path = tmp_path / "v3.db"
    backup_path = tmp_path / "before-migration.db"
    _create_v3(path, [
        ("EURUSD", "BUY", 0.75, 100, "2026-10-17T10:00:00+00:00"),
    ])
    backup_journal(path, backup_path)
    original_v4 = MIGRATIONS[4]
    monkeypatch.setitem(
        MIGRATIONS, 4, [*original_v4, "THIS IS NOT VALID SQL"])

    with pytest.raises(sqlite3.OperationalError, match="syntax error"):
        Journal(path)

    assert _read(path, "SELECT MAX(version) FROM schema_version") == [(3,)]
    assert _read(path, "SELECT id, symbol FROM signals") == [(1, "EURUSD")]
    assert _read(path, "SELECT name FROM sqlite_master "
                       "WHERE type='table' AND name='signals_v4'") == []
    assert _read(backup_path, "SELECT id, symbol FROM signals") == [(1, "EURUSD")]
    assert _read(backup_path, "PRAGMA integrity_check") == [("ok",)]


def test_v4_migration_table_has_expected_schema_version():
    assert SCHEMA_VERSION == 4
    assert journal_module.MIGRATIONS[4] is journal_module.MIGRATION_4
