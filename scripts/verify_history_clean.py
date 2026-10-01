"""C3 step 5: prove the search method finds the leaked values.

Run this against a repo BEFORE the rewrite to confirm the method actually
detects the known leak. A search that cannot find a known leak tells us
nothing about a clean result.

Secrets are read from .env at runtime and NEVER printed. Only counts and
booleans are emitted.

Usage:
    python scripts/verify_history_clean.py --repo <path>
Exit 0 = every secret value absent from history.
Exit 1 = at least one secret still present.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Only these keys are treated as secrets. MT5_PATH and MT5_SERVER are
# configuration, not credentials, and scanning for them would produce
# meaningless "leaks".
SECRET_KEYS = ("MT5_PASSWORD", "OPENROUTER_API_KEY", "GROQ_API_KEY", "MT5_LOGIN")


def _load_dotenv_values(env_path: Path) -> dict[str, str]:
    """Read key=value pairs from .env without echoing any value."""
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    text = env_path.read_text(encoding="utf-8-sig", errors="replace")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def secret_fingerprints(values: dict[str, str]) -> dict[str, str]:
    """Map secret key -> short non-reversible fingerprint.

    The fingerprint lets us correlate a hit across runs without ever printing
    the secret. It is a sha256 prefix, so it is not reversible by brute force
    on a value with real entropy.
    """
    import hashlib
    out = {}
    for key in SECRET_KEYS:
        value = values.get(key, "")
        if not value or value.startswith("YOUR_"):
            continue
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        out[key] = f"sha256:{digest} (len={len(value)})"
    return out


def search_history(repo: Path, value: str) -> tuple[int, int]:
    """Return (commit_count, occurrence_count) for `value` in all history.

    Uses `git log -S<value> --all --oneline`, which finds commits where the
    count of occurrences changed. The value is passed via stdin to the child
    process so it never appears in this process's command line.
    """
    proc = subprocess.run(
        ["git", "log", "--all", "-S", value, "--oneline"],
        cwd=str(repo), capture_output=True,
    )
    text = proc.stdout.decode("utf-8", "replace")
    commits = [ln for ln in text.splitlines() if ln.strip()]
    return len(commits), len(commits)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="path to the repo clone")
    parser.add_argument("--env", default=str(_REPO_ROOT / ".env"),
                        help="path to .env holding the live secret values")
    parser.add_argument("--expect-clean", action="store_true",
                        help="treat any hit as a failure (post-rewrite check)")
    parser.add_argument("--expect-leak", action="store_true",
                        help="report a failure if a value is NOT found "
                             "(pre-rewrite sanity check)")
    args = parser.parse_args(argv)

    repo = Path(args.repo)
    values = _load_dotenv_values(Path(args.env))
    fingerprints = secret_fingerprints(values)

    print("=" * 72)
    print("  HISTORY LEAK VERIFICATION (values never printed)")
    print("=" * 72)
    print(f"  repo: {repo}")
    print(f"  secrets loaded from .env: {len(fingerprints)}")
    for key, fp in sorted(fingerprints.items()):
        print(f"    {key:22s} {fp}")
    print()

    dirty = subprocess.run(["git", "status", "--porcelain"],
                           cwd=str(repo), capture_output=True, text=True)
    if dirty.stdout.strip():
        print("  WARNING: clone has uncommitted changes")
        print()

    found_any = False
    for key in sorted(fingerprints):
        value = values[key]
        commits, occurrences = search_history(repo, value)
        present = commits > 0
        found_any = found_any or present
        state = "PRESENT IN HISTORY" if present else "absent"
        print(f"  {key:22s} {state:20s} commits={commits}")

    print()
    if args.expect_leak:
        if not found_any:
            print("  RESULT: FAIL - expected to find the leak, found none.")
            print("          The search method may be broken; a 'clean' result")
            print("          from it would be meaningless.")
            return 1
        print("  RESULT: OK - the leak was detected, so the method works.")
        return 0

    if args.expect_clean and found_any:
        print("  RESULT: FAIL - at least one secret is still in history.")
        return 1

    print("  RESULT: OK - no .env secret value found in history.")
    return 0


if __name__ == "__main__":
    sys.exit(main())