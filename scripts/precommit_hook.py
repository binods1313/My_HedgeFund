#!/usr/bin/env python
"""Pre-commit hook: block secrets from being committed.

Runs two independent controls:

  1. scripts/secret_scan.py --verify  — proves the pattern set still matches
     planted canaries. If the scanner has rotted, the commit is blocked rather
     than silently allowed through on a false "clean".
  2. scripts/secret_scan.py on the staged blob content — flags any credential.

detect-secrets is wired in via .pre-commit-config.yaml for the official rule
set; this hook is the fast, self-validating gate that does not depend on it
being installed.

Install:  python scripts/install_precommit_hook.py
Bypass:   git commit --no-verify   (avoid; CI re-runs the same scan)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def staged_files() -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def staged_blob(path: str) -> str:
    """Content of a file as it will be committed (the staged blob)."""
    proc = subprocess.run(
        ["git", "show", f":{path}"],
        cwd=str(REPO_ROOT), capture_output=True,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", "replace")


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT))
    from scripts import secret_scan as ss

    if not ss.verify_scanner_detects_known_secret():
        print("[FAIL] Secret scanner self-test failed.")
        print("       The scanner no longer detects known secret shapes, so it")
        print("       cannot be trusted to clear this commit.")
        print("       Fix scripts/secret_scan.py PATTERNS or bypass deliberately.")
        return 1

    findings: list[str] = []
    scanned = 0
    for rel in staged_files():
        # The scanner and its tests must contain synthetic canaries. The list
        # lives with the scanner so the two cannot drift apart.
        if rel in ss.SELF_REFERENTIAL_FILES:
            continue
        content = staged_blob(rel)
        if not content:
            continue
        scanned += 1
        for n, line in enumerate(content.splitlines(), start=1):
            for name, pattern in ss.PATTERNS:
                for m in pattern.finditer(line):
                    value = m.groupdict().get("val") or m.group(0)
                    if ss.is_placeholder(value) or ss.is_env_reference(value):
                        continue
                    findings.append(
                        f"  {rel}:{n}  [{name}]  value={ss.mask(value)}\n"
                        f"      {ss.redact_line(line.strip())}"
                    )

    if findings:
        print("[FAIL] Possible secrets in staged changes:\n")
        print("\n".join(findings))
        print("\n  Move real credentials to .env (gitignored). Use placeholders in")
        print("  .env.example and docs. Commit aborted.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())