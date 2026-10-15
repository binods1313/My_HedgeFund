"""Guards on scripts/purge_engine_db.py. These tests never open the live journal."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import scripts.purge_engine_db as purge
from engine.journal import Journal

ACCOUNT = "999999999"
FILENAME = f"engine_{ACCOUNT}.db"


def _db(tmp_path: Path) -> Path:
    path = tmp_path / FILENAME
    journal = Journal(path)
    journal.close()
    return path


def _fresh_backup(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / "engine_pre.db"
    backup.write_bytes(b"backup")
    return backup


def test_both_confirmations_are_required(tmp_path):
    db = _db(tmp_path)
    backups = tmp_path / "backups"
    _fresh_backup(backups)
    assert purge.decide_purge(db, backups, "", FILENAME) is not None
    assert purge.decide_purge(db, backups, ACCOUNT, "") is not None
    assert purge.decide_purge(db, backups, "000", FILENAME) is not None
    assert purge.decide_purge(db, backups, ACCOUNT, "engine.db") is not None
    assert purge.decide_purge(db, backups, ACCOUNT, FILENAME) is None
    assert db.exists()


def test_a_missing_backup_and_a_stale_backup_refuse(tmp_path):
    db = _db(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    reason = purge.decide_purge(db, empty, ACCOUNT, FILENAME)
    assert reason is not None
    assert "backup" in reason

    stale = tmp_path / "stale"
    backup = _fresh_backup(stale)
    old = time.time() - (purge.BACKUP_MAX_AGE_SECONDS + 60)
    os_utime = backup.stat()
    assert os_utime.st_mtime > 0
    import os
    os.utime(backup, (old, old))
    reason = purge.decide_purge(db, stale, ACCOUNT, FILENAME)
    assert reason is not None
    assert "one hour" in reason
    assert db.exists()


def test_a_held_lock_refuses_even_with_a_fresh_backup(tmp_path):
    db = _db(tmp_path)
    backups = tmp_path / "backups"
    _fresh_backup(backups)
    lock = db.parent / f"{db.stem}.lock"
    lock.write_bytes(b"12345")
    holder = tmp_path / "hold_lock.py"
    holder.write_text(
        "import sys, time\n"
        "import msvcrt\n"
        "handle = open(sys.argv[1], 'a+b')\n"
        "handle.seek(0, 2)\n"
        "msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)\n"
        "handle.seek(0)\n"
        "handle.truncate()\n"
        "handle.write(b'12345')\n"
        "handle.flush()\n"
        "print('HELD', flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(holder), str(lock)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = process.stdout.readline()
        assert line.startswith("HELD")
        assert purge.lock_is_held(lock) is True
        reason = purge.decide_purge(db, backups, ACCOUNT, FILENAME)
        assert reason is not None
        assert "lock" in reason
        assert db.exists()
    finally:
        process.kill()
        process.wait(timeout=5)


def test_an_unlocked_file_is_not_a_held_lock(tmp_path):
    lock = tmp_path / "engine_999999999.lock"
    lock.write_bytes(b"12345")
    assert purge.lock_is_held(lock) is False


def test_yes_does_not_skip_confirmations_and_a_temp_purge_deletes(tmp_path, monkeypatch):
    db = _db(tmp_path)
    backups = tmp_path / "backups"
    _fresh_backup(backups)
    monkeypatch.setattr(purge, "configured_db", lambda: db)
    monkeypatch.setattr(purge, "ARCHIVE_DIR", backups)

    def _no_input(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _no_input)
    assert purge.main(["--yes"]) == 1
    assert db.exists()

    assert purge.main([
        "--yes",
        "--confirm-account", ACCOUNT,
        "--confirm-filename", FILENAME,
    ]) == 0
    assert not db.exists()
    archives = list(backups.glob("engine_pre_purge_*.db"))
    assert len(archives) == 1
