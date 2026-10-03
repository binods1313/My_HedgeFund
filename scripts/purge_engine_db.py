"""Purge the engine database, keeping an archived copy.

The engine currently mirrors two positions that predate the work. A stale
mirror is not merely untidy: a mirror that silently disagrees with the broker
is exactly the failure that made positions_get(None) dangerous.

The archive is kept rather than deleted so the previous state can be inspected
if the purge turns out to have hidden something.

Usage:
    .venv\\Scripts\\python.exe scripts\\purge_engine_db.py
    .venv\\Scripts\\python.exe scripts\\purge_engine_db.py --yes
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB = REPO_ROOT / "data" / "engine.db"
ARCHIVE_DIR = REPO_ROOT / "backups"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt")
    args = parser.parse_args(argv)

    print("=" * 70)
    print("  ENGINE DATABASE PURGE")
    print("=" * 70)

    if not DB.exists():
        print(f"  nothing to purge: {DB} does not exist")
        return 0

    print(f"  target: {DB}")
    print()
    print("  current contents:")
    print(summarise(DB))
    print()

    if not args.yes:
        print("  This removes the engine's mirror of broker positions.")
        print("  Re-running the engine will re-adopt whatever is open at the")
        print("  broker, so the mirror repopulates on next start.")
        print()
        answer = input("  Type PURGE to continue: ").strip()
        if answer != "PURGE":
            print("  aborted; nothing changed")
            return 1

    # Archive before deleting, so the previous state remains inspectable.
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive = ARCHIVE_DIR / f"engine_pre_purge_{stamp}.db"
    shutil.copy2(DB, archive)

    # Remove the database and its WAL/SHM sidecars. Leaving them behind would
    # let SQLite resurrect committed pages from the old file.
    removed = []
    for path in (DB, Path(str(DB) + "-wal"), Path(str(DB) + "-shm")):
        if path.exists():
            path.unlink()
            removed.append(path.name)

    print()
    print(f"  archived to : {archive}")
    print(f"  removed     : {', '.join(removed)}")
    print()
    print("  RESULT: engine database purged. It will be recreated, with the")
    print("          schema migrated from scratch, on the next engine start.")
    return 0


if __name__ == "__main__":
    sys.exit(main())