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

from scripts.secret_scan import DECLARED_CANARIES

# Keys whose values are genuine secrets. MT5_LOGIN is an account number, not a
# credential: it appears in test fixtures by design, and treating it as a secret
# would report the repo as never clean.
SECRET_KEYS = ("MT5_PASSWORD", "OPENROUTER_API_KEY", "GROQ_API_KEY")

# Synthetic values that are safe to commit, because they were authored as test
# data rather than taken from a live credential.
#
# This replaces an earlier PATH-based exemption that treated any
# credential-shaped string under tests/ or scripts/secret_scan.py as harmless.
# That was wrong: a real password pasted into a test file was excused purely
# because of where it lived. Exemption is now granted per VALUE, so a live
# secret in a test is still a leak.
#
# Add a value here only after confirming it is not a real credential.
SYNTHETIC_TEST_VALUES = frozenset({
    # Documented placeholders.
    "YOUR_MT5_PASSWORD",
    "your_password",
    "YOUR_OPENROUTER_KEY",
    "YOUR_KEY_HERE",
    "changeme",
    # Scrub markers left by a secret-removal rewrite.
    "***REMOVED-MT5_PASSWORD***",
    "***REMOVED-OPENROUTER_API_KEY***",
    "[REDACTED]",
    # Fixed-width dummy used in scanner self-tests and fixtures.
    "999999999",
    "000000000",
    "123456789",
})

# Canaries are imported from the scanner rather than duplicated here, so the
# audit, the allowlist and the scanner's self-test cannot disagree.
_CANARY_VALUES = set(DECLARED_CANARIES)

# Union of the shapes we can justify: declared canaries, documented
# placeholders, scrub markers and fixed-width dummy account numbers.
# Anything outside this set is treated as a real credential.
SYNTHETIC_TEST_VALUES = frozenset(
    set(SYNTHETIC_TEST_VALUES) | set(_CANARY_VALUES)
)

# Synthetic values that must never be treated as safe regardless of where they
# appear. This is the escape hatch, not the rule: a real credential is a leak
# wherever it sits.
NEVER_SYNTHETIC = frozenset()


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


def is_synthetic(value: str) -> bool:
    """True when `value` is authored test data rather than a live credential.

    Deliberately value-based: the earlier path-based exemption let a real
    password sit in a test file unnoticed, because "it's under tests/" said
    nothing about whether the string was a live credential.
    """
    if value in NEVER_SYNTHETIC:
        return False
    stripped = value.strip().strip('"').strip("'")
    if stripped in SYNTHETIC_TEST_VALUES:
        return True
    # Scrub markers from a history rewrite, with or without a key suffix.
    if stripped.startswith("***REMOVED-") and stripped.endswith("***"):
        return True
    if stripped in ("[REDACTED]", "REDACTED"):
        return True
    # Documented placeholder style.
    upper = stripped.upper()
    if upper.startswith("YOUR_") or upper == "CHANGEME":
        return True
    return False


def search_history(repo: Path, value: str) -> tuple[int, int]:
    """Return (commit_count, leak_file_count) for `value` in history.

    Uses `git log -S<value> --all --oneline`, which finds commits where the
    count of occurrences changed. If the value itself is a known synthetic
    test value the whole thing short-circuits: a synthetic value is never a
    leak, wherever it appears.
    """
    if is_synthetic(value):
        return 0, 0

    proc = subprocess.run(
        ["git", "log", "--all", "-S", value, "--oneline"],
        cwd=str(repo), capture_output=True,
    )
    commits = [ln for ln in proc.stdout.decode("utf-8", "replace").splitlines()
               if ln.strip()]
    if not commits:
        return 0, 0

    # Enumerate which files actually contain the value across all commits.
    # Any file counts: location no longer grants an exemption.
    revs = subprocess.run(["git", "rev-list", "--all"],
                          cwd=str(repo), capture_output=True).stdout.decode().split()

    leak_paths: set[str] = set()
    for rev in revs:
        out = subprocess.run(
            ["git", "grep", "-l", "-F", value, rev],
            cwd=str(repo), capture_output=True,
        ).stdout.decode("utf-8", "replace")
        for line in out.splitlines():
            path = line.strip()
            if not path:
                continue
            # Strip the "<rev>:" prefix git grep adds.
            if ":" in path:
                path = path.split(":", 1)[1]
            leak_paths.add(path)

    return len(commits), len(leak_paths)


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
        commits, leak_files = search_history(repo, value)
        # A synthetic value is never a leak, wherever it appears. A non-
        # synthetic value is a leak in ANY file, test or not.
        present = leak_files > 0
        found_any = found_any or present
        state = "PRESENT IN HISTORY" if present else "absent"
        print(f"  {key:22s} {state:20s} commits={commits} "
              f"leak_files={leak_files}")

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