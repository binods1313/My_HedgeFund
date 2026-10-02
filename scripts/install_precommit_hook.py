"""Install the secret-scanning pre-commit hook for this repository.

Creates .git/hooks/pre-commit (or overwrites an existing one, after backing it
up) pointing at scripts/precommit_hook.py.

Usage:
    python scripts/install_precommit_hook.py
    python scripts/install_precommit_hook.py --uninstall
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / ".git" / "hooks" / "pre-commit"

HOOK_TEMPLATE = """#!/bin/sh
# Secret-scanning pre-commit hook (installed by scripts/install_precommit_hook.py)
# Blocks commits containing credentials, and blocks commits when the scanner
# itself has stopped detecting known secret shapes.
exec "{python}" "{hook}" "$@"
"""


def install() -> int:
    if not (REPO_ROOT / ".git").exists():
        print("Not a git repository; nothing to install.", file=sys.stderr)
        return 1

    HOOK_PATH.parent.mkdir(parents=True, exist_ok=True)

    if HOOK_PATH.exists():
        backup = HOOK_PATH.with_suffix(".bak")
        shutil.copy2(HOOK_PATH, backup)
        print(f"Existing hook backed up to {backup.name}")

    HOOK_PATH.write_text(
        HOOK_TEMPLATE.format(python=sys.executable, hook=REPO_ROOT / "scripts" / "precommit_hook.py"),
        encoding="utf-8",
    )
    # Make executable on POSIX; a no-op on Windows.
    try:
        HOOK_PATH.chmod(HOOK_PATH.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    except OSError:
        pass

    print(f"Installed pre-commit hook -> {HOOK_PATH}")
    print("It runs scripts/precommit_hook.py on every commit.")
    return 0


def uninstall() -> int:
    if not HOOK_PATH.exists():
        print("No pre-commit hook installed.")
        return 0
    HOOK_PATH.unlink()
    print(f"Removed {HOOK_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)
    return uninstall() if args.uninstall else install()


if __name__ == "__main__":
    sys.exit(main())