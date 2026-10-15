"""Purge the engine database, keeping an archived copy.

The archive is kept so the previous state can be inspected if the purge
turns out to have hidden something.

The purge refuses unless the operator confirms both the account number and
the database filename. ``--yes`` does not skip those confirmations. It also
refuses while the engine lock is held, and when the newest backup is missing
or older than one hour.

Usage:
    .venv\\Scripts\\python.exe scripts\\purge_engine_db.py
    .venv\\Scripts\\python.exe scripts\\purge_engine_db.py --yes ^
        --confirm-account 5057151780 ^
        --confirm-filename engine_5057151780.db
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = REPO_ROOT / "backups"
BACKUP_MAX_AGE_SECONDS = 3600


def configured_db() -> Path:
    """The journal the engine is actually configured to use.

    This script once hardcoded ``data/engine.db``. After the account switch the
    configured journal became a different file, so the literal would have
    purged the wrong file while reporting success.
    """
    from engine.config import load_engine_config
    from engine.journal import resolve_db_path
    return resolve_db_path(load_engine_config().engine.db_path)


def account_number_from_db(db: Path) -> str:
    """The digits in ``engine_<account>.db``. Anything else confirms as empty."""
    stem = db.stem
    prefix = "engine_"
    if stem.startswith(prefix) and stem[len(prefix):].isdigit():
        return stem[len(prefix):]
    return ""


def newest_backup_age_seconds(backup_dir: Path, *,
                              now: float | None = None) -> float | None:
    """Age of the newest ``*.db`` under ``backup_dir``, or None when absent."""
    if not backup_dir.exists():
        return None
    files = [path for path in backup_dir.rglob("*.db") if path.is_file()]
    if not files:
        return None
    newest = max(path.stat().st_mtime for path in files)
    clock = time.time() if now is None else now
    return clock - newest


def lock_is_held(lock_path: Path) -> bool:
    """True when any byte of the lock file, including one past the end, is held.

    The engine locks one byte at the ``a+b`` offset and then truncates and
    rewrites the pid. That byte can sit at the previous length, so a probe of
    byte 0 alone can miss it. A missing file is not a held lock.
    """
    if not lock_path.exists():
        return False
    size = lock_path.stat().st_size
    span = max(size + 1, 1)
    try:
        handle = open(lock_path, "rb")  # noqa: PTH123 - lock probe, not data
    except OSError:
        return True
    locked: list[int] = []
    try:
        if os.name == "nt":
            import msvcrt
            for offset in range(span):
                try:
                    handle.seek(offset)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return True
                locked.append(offset)
            return False
        import fcntl
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            return True
        return False
    finally:
        if os.name == "nt" and locked:
            import msvcrt
            for offset in locked:
                try:
                    handle.seek(offset)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        try:
            handle.close()
        except OSError:
            pass


def decide_purge(db: Path, backup_dir: Path, confirm_account: str,
                 confirm_filename: str, *,
                 now: float | None = None) -> str | None:
    """Return a refusal reason, or None when a purge is allowed.

    None means every guard passed. Callers delete only after that. This
    function does not delete and does not open the journal.
    """
    expected_account = account_number_from_db(db)
    if not confirm_account or confirm_account != expected_account:
        return "account confirmation does not match the database"
    if not confirm_filename or confirm_filename != db.name:
        return "filename confirmation does not match the database"
    if lock_is_held(db.parent / f"{db.stem}.lock"):
        return "the engine lock is held; refusing to purge"
    age = newest_backup_age_seconds(backup_dir, now=now)
    if age is None:
        return "no backup is available"
    if age > BACKUP_MAX_AGE_SECONDS:
        return "the newest backup is older than one hour"
    return None


def summarise(db: Path) -> str:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    lines = []
    for label, sql in (
        ("positions", "SELECT COUNT(*) n FROM positions"),
        ("trades", "SELECT COUNT(*) n FROM trades"),
        ("events", "SELECT COUNT(*) n FROM events"),
        ("commands", "SELECT COUNT(*) n FROM commands"),
        ("heartbeats", "SELECT COUNT(*) n FROM heartbeats"),
    ):
        row = conn.execute(sql).fetchone()
        lines.append(f"    {label:14s} {row['n'] if row and row['n'] is not None else 0}")

    rows = conn.execute(
        "SELECT ticket, symbol, side, volume FROM positions ORDER BY ticket"
    ).fetchall()
    if rows:
        lines.append("    mirrored positions:")
        for row in rows:
            lines.append(
                f"      ticket={row['ticket']} {row['symbol']} {row['side']} "
                f"vol={row['volume']}")
    conn.close()
    return "\n".join(lines)


def _prompt(label: str) -> str:
    try:
        return input(label).strip()
    except EOFError:
        return ""


def purge_files(db: Path, archive_dir: Path) -> tuple[Path, list[str]]:
    """Copy ``db`` into ``archive_dir`` and remove the journal and its sidecars.

    The caller has already passed ``decide_purge``. This does not re-check
    the lock; do not call it against a journal an engine still has open.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive = archive_dir / f"engine_pre_purge_{stamp}.db"
    shutil.copy2(db, archive)
    removed = []
    for path in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
        if path.exists():
            path.unlink()
            removed.append(path.name)
    return archive, removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true",
                        help="skip the PURGE prompt; account and filename "
                             "confirmations are still required")
    parser.add_argument("--confirm-account", default="",
                        help="account number taken from the database filename")
    parser.add_argument("--confirm-filename", default="",
                        help="database filename, including the .db suffix")
    args = parser.parse_args(argv)

    print("=" * 70)
    print("  ENGINE DATABASE PURGE")
    print("=" * 70)

    db = configured_db()
    account = args.confirm_account or _prompt("  Type the account number: ")
    filename = args.confirm_filename or _prompt("  Type the database filename: ")
    reason = decide_purge(db, ARCHIVE_DIR, account, filename)
    if reason:
        print(f"  refused: {reason}")
        print("  aborted; nothing changed")
        return 1

    if not db.exists():
        print(f"  nothing to purge: {db} does not exist")
        return 0

    print(f"  target: {db}")
    print()
    print("  current contents:")
    print(summarise(db))
    print()

    if not args.yes:
        print("  This removes the engine's mirror of broker positions.")
        print("  Re-running the engine will re-adopt whatever is open at the")
        print("  broker, so the mirror repopulates on next start.")
        print()
        answer = _prompt("  Type PURGE to continue: ")
        if answer != "PURGE":
            print("  aborted; nothing changed")
            return 1

    archive, removed = purge_files(db, ARCHIVE_DIR)
    print()
    print(f"  archived to : {archive}")
    print(f"  removed     : {', '.join(removed)}")
    print()
    print("  RESULT: engine database purged. It will be recreated, with the")
    print("          schema migrated from scratch, on the next engine start.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
