"""Classify history secret-scan hits without printing the match.

KNOWN_CANARY requires both the file and membership in DECLARED_CANARIES.
The two files are scripts/secret_scan.py and tests/test_secret_scan.py.
Anything else is UNKNOWN. This script does not rewrite history.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.secret_scan import (
    DECLARED_CANARIES,
    PATTERNS,
    _git,
    _is_benign_line,
    iter_commits,
)

CANARY_FILES = frozenset({
    "scripts/secret_scan.py",
    "tests/test_secret_scan.py",
})


def classify(path: str, value: str) -> str:
    normalized = path.replace("\\", "/").lstrip("./")
    if normalized in CANARY_FILES and value in DECLARED_CANARIES:
        return "KNOWN_CANARY"
    return "UNKNOWN"


def match_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def format_row(path: str, commit: str, rule: str, value: str) -> str:
    """file, commit, rule, hash, class. The match itself is not included."""
    return "\t".join((path, commit, rule, match_hash(value), classify(path, value)))


def iter_classified(repo: Path):
    """Same diff walk as scan_commit. Yields rows and never yields the match."""
    for sha in iter_commits(repo):
        code, out, _err = _git(
            ["show", "--format=", "--unified=0", "--no-color", sha], repo
        )
        if code != 0:
            continue
        text = out.decode("utf-8", "replace")
        current_file = ""
        for line in text.splitlines():
            if line.startswith("+++ b/"):
                current_file = line[6:].strip()
                continue
            if line.startswith("@@"):
                continue
            if line.startswith("+") and not line.startswith("+++"):
                content = line[1:]
                for name, pattern in PATTERNS:
                    for found in pattern.finditer(content):
                        value = found.groupdict().get("val") or found.group(0)
                        if _is_benign_line(line, value):
                            continue
                        yield format_row(current_file, sha[:12], name, value)


def main() -> int:
    known = 0
    unknown = 0
    unknown_rows: list[str] = []
    for row in iter_classified(ROOT):
        print(row)
        if row.endswith("\tKNOWN_CANARY"):
            known += 1
        else:
            unknown += 1
            unknown_rows.append(row)
    print(f"KNOWN_CANARY\t{known}")
    print(f"UNKNOWN\t{unknown}")
    print(f"TOTAL\t{known + unknown}")
    if unknown_rows:
        print("UNKNOWN list")
        for row in unknown_rows:
            print(row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
