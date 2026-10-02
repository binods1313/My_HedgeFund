"""Filter history scan output to the hits that actually matter.

A secret that only ever appears inside the scanner's own canaries or its tests
is not a leak -- it is synthetic material we deliberately committed so the
scanner can prove it still fires. This reports the two groups separately so a
reviewer does not have to eyeball 23 lines to find the one real problem.

Usage:
    python scripts/secret_scan.py --json | python scripts/secret_triage.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.secret_scan import SELF_REFERENTIAL_FILES


def triage(hits: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split hits into (real_leaks, self_referential)."""
    real, self_ref = [], []
    for hit in hits:
        if hit["file"] in SELF_REFERENTIAL_FILES:
            self_ref.append(hit)
        else:
            real.append(hit)
    return real, self_ref


def main() -> int:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("expected JSON from `python scripts/secret_scan.py --json` on stdin",
              file=sys.stderr)
        return 2

    all_hits = data.get("history_hits", []) + data.get("worktree_hits", [])
    real, self_ref = triage(all_hits)

    print("=" * 72)
    print("  SECRET SCAN TRIAGE")
    print("=" * 72)
    print(f"  Scanner self-test : {'PASS' if data.get('scanner_self_test_passed') else 'FAIL'}")
    print(f"  Total hits        : {len(all_hits)}")
    print(f"  Real leaks        : {len(real)}")
    print(f"  Self-referential  : {len(self_ref)}  (scanner canaries + their tests)")
    print()

    if real:
        print("--- REAL SECRETS REQUIRING ACTION ---")
        for h in real:
            print(f"  {h['pattern']}")
            print(f"    commit : {h['commit']}")
            print(f"    file   : {h['file']}:{h['line_no']}")
            print(f"    value  : {h['masked_value']}")
            print()
    else:
        print("  No real secrets found in history or working tree.")

    if self_ref:
        print()
        print("--- Self-referential (expected) ---")
        by_file: dict[str, int] = {}
        for h in self_ref:
            by_file[h["file"]] = by_file.get(h["file"], 0) + 1
        for f, n in sorted(by_file.items()):
            print(f"  {f}: {n} hit(s)")

    # Exit non-zero only on a real leak, so this can gate CI.
    return 1 if real else 0


if __name__ == "__main__":
    sys.exit(main())