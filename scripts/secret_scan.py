"""Secret scanner for git history and the working tree.

Purpose: produce a defensible list of credential hits across ALL refs, with
values masked. This exists because a naive `git log | grep` is unreliable on
Windows (cp1252 decode errors, silently empty output) and once produced a
false "clean" result.

Reliability is verified, not assumed: `verify_scanner_detects_known_secret()`
proves the pattern set actually matches a planted dummy secret before any
"no hits found" conclusion is reported.

Usage:
    python scripts/secret_scan.py                     # scan history + tree
    python scripts/secret_scan.py --verify            # self-test only
    python scripts/secret_scan.py --json              # machine-readable

Run: python -m pytest tests/test_secret_scan.py -v
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# Patterns
# ─────────────────────────────────────────────────────────────────────────────

# Each pattern is (name, compiled regex). Group 1, when present, is the secret.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("openrouter_key", re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("groq_key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # Assignment form: KEY = "value" where the key name looks sensitive.
    #
    # No leading \b: '_' is a word character, so \bPASSWORD does NOT match
    # inside "MT5_PASSWORD" (no boundary between '5' and '_'). A leading \b
    # here silently missed every credential in this repo. The keyword list is
    # matched mid-identifier instead, with a trailing \b so it cannot match a
    # longer word such as "passwordless".
    (
        "credential_assignment",
        re.compile(
            r"(?i)(?:password|passwd|pwd|secret|api[_-]?key|apikey|token|"
            r"access[_-]?key|client[_-]?secret)\b"
            r"\s*[:=]\s*"
            r"[\"']?(?P<val>[^\s\"'#,}]{6,})[\"']?"
        ),
    ),
]

# Values that are obviously placeholders, not real credentials.
PLACEHOLDER_VALUES = {
    "your_password", "your_mt5_password", "your_openrouter_key", "your_api_key",
    "your_mt5_account_number", "your_broker_server", "your_token", "your_secret",
    "yourkeyhere", "your_key_here", "changeme", "placeholder", "example",
    "your_openrouter_api_key", "none", "null", "true", "false", "xxxxx",
}


def mask(value: str) -> str:
    """Mask a secret for display: keep a short prefix and suffix only."""
    v = value.strip().strip('"').strip("'")
    if len(v) <= 8:
        return "*" * len(v)
    return f"{v[:4]}{'*' * (len(v) - 8)}{v[-4:]}"


def is_placeholder(value: str) -> bool:
    """True when the value is a documented placeholder rather than a secret."""
    v = value.strip().strip('"').strip("'").lower()
    if v in PLACEHOLDER_VALUES:
        return True
    # YOUR_X / YOUR-X style placeholders, with or without a trailing word.
    if re.match(r"^your[-_ ]?\w*$", v):
        return True
    # All-same-character filler.
    if len(v) > 2 and len(set(v)) == 1 and not v.isalnum():
        return True
    # Mostly asterisks / x's used as redaction.
    if set(v) <= {"*", "x", "X", ".", "0"} and len(v) >= 6:
        return True
    # Truncated examples: "sk-or-v1-...", "gsk_...", "<your-key-here>".
    # A real key has a long high-entropy tail; an ellipsis does not.
    if "..." in v or "…" in v:
        return True
    if v.startswith("<") and v.endswith(">"):
        return True
    # Redaction / scrub markers left by a secret-removal rewrite, e.g.
    # "***REMOVED-MT5_PASSWORD***" or "[REDACTED]". These are the intended
    # output of scrubbing, not a credential.
    if re.search(r"removed[-_ ]?(?:secret|password|key|token)?", v):
        return True
    if "redacted" in v or "scrubbed" in v:
        return True
    return False


# Values that are code references to the environment, not credentials.
# "MT5_PASSWORD = os.environ['MT5_PASSWORD']" is the correct way to read a
# secret; flagging it is noise that trains people to ignore the scanner.
_ENV_REFERENCE_MARKERS = (
    "os.environ", "getenv", "environ.get", "environ[", "config.",
    "dotenv", "getattr(", "load_dotenv",
)

# Environment variable names are uppercase identifiers. A value matching this
# is a variable reference (e.g. "password=MT5_PASSWORD"), not a literal secret.
_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")


def is_env_reference(value: str) -> bool:
    """True when the value is code that READS a secret, not a literal one.

    Covers both `os.environ[...]` style access and passing an uppercase
    variable name (MT5_PASSWORD=MT5_PASSWORD), which is how the value flows
    from config.py into the MT5 call. Neither is a credential.
    """
    stripped = value.strip().strip('"').strip("'")
    if any(marker in value for marker in _ENV_REFERENCE_MARKERS):
        return True
    return bool(_VAR_NAME_RE.match(stripped))


@dataclass(frozen=True)
class Hit:
    pattern: str
    commit: str
    file: str
    line_no: int
    masked_value: str
    line: str

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Git helpers
# ─────────────────────────────────────────────────────────────────────────────

def _git(args: list[str], cwd: Path) -> tuple[int, bytes, bytes]:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, timeout=600,
    )
    return proc.returncode, proc.stdout, proc.stderr


def iter_commits(repo: Path) -> list[str]:
    """All commit SHAs across every ref, including remote-tracking branches."""
    code, out, err = _git(["rev-list", "--all"], repo)
    if code != 0:
        raise RuntimeError(f"git rev-list --all failed: {err.decode('utf-8', 'replace')}")
    return out.decode("utf-8", "replace").split()


def commit_meta(repo: Path, sha: str) -> tuple[str, str]:
    """Return (author_date, subject) for a commit."""
    code, out, _ = _git(["show", "-s", "--format=%aI%x1f%s", sha], repo)
    if code != 0:
        return ("", "")
    text = out.decode("utf-8", "replace").strip()
    date, _, subject = text.partition("\x1f")
    return (date, subject)


def scan_commit(repo: Path, sha: str) -> list[Hit]:
    """Scan one commit's diff for secrets, attributing each hit to a file."""
    code, out, err = _git(
        ["show", "--format=", "--unified=0", "--no-color", sha], repo
    )
    if code != 0:
        return []

    text = out.decode("utf-8", "replace")
    hits: list[Hit] = []
    current_file = ""
    line_no = 0

    for line in text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            continue
        if line.startswith("@@"):
            # @@ -a,b +c,d @@  -> new-file line number
            m = re.search(r"\+(\d+)", line)
            line_no = int(m.group(1)) if m else 0
            continue
        if line.startswith("+") and not line.startswith("+++"):
            content = line[1:]
            for name, pattern in PATTERNS:
                for m in pattern.finditer(content):
                    value = m.groupdict().get("val") or m.group(0)
                    if is_placeholder(value) or is_env_reference(value):
                        continue
                    hits.append(Hit(
                        pattern=name,
                        commit=sha[:12],
                        file=current_file,
                        line_no=line_no,
                        masked_value=mask(value),
                        line=content.strip()[:160],
                    ))
            line_no += 1
    return hits


def scan_history(repo: Path) -> list[Hit]:
    """Scan every commit on every ref."""
    hits: list[Hit] = []
    for sha in iter_commits(repo):
        hits.extend(scan_commit(repo, sha))
    return hits


# Files that legitimately contain credential-shaped strings: the scanner needs
# synthetic canaries to prove it still fires, and its tests need sample secrets
# for the same reason. Kept in one place so the pre-commit hook and the tests
# cannot drift apart.
SELF_REFERENTIAL_FILES = frozenset({
    "scripts/secret_scan.py",
    "scripts/precommit_hook.py",
    "tests/test_secret_scan.py",
    ".secrets.baseline",
})


def scan_tree(repo: Path) -> list[Hit]:
    """Scan the current working tree (tracked files only).

    Self-referential files listed in SELF_REFERENTIAL_FILES are skipped; their
    credential-shaped content is synthetic by construction.
    """
    code, out, err = _git(["ls-files"], repo)
    if code != 0:
        raise RuntimeError(f"git ls-files failed: {err.decode('utf-8', 'replace')}")

    hits: list[Hit] = []
    for rel in out.decode("utf-8", "replace").split():
        if rel in SELF_REFERENTIAL_FILES:
            continue
        path = repo / rel
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for n, line in enumerate(content.splitlines(), start=1):
            for name, pattern in PATTERNS:
                for m in pattern.finditer(line):
                    value = m.groupdict().get("val") or m.group(0)
                    if is_placeholder(value) or is_env_reference(value):
                        continue
                    hits.append(Hit(
                        pattern=name,
                        commit="WORKTREE",
                        file=rel,
                        line_no=n,
                        masked_value=mask(value),
                        line=line.strip()[:160],
                    ))
    return hits


def scan_git_config(repo: Path) -> list[Hit]:
    """Scan .git/config for credentials embedded in remote URLs.

    A token in an https remote URL (`https://<token>@github.com/...`) is a
    live credential that never appears in any commit, so a history-only scan
    reports the repo as clean while the token sits in plain sight on disk.
    This checks it explicitly.
    """
    config = repo / ".git" / "config"
    if not config.exists():
        return []

    hits: list[Hit] = []
    for n, line in enumerate(config.read_text(encoding="utf-8",
                                              errors="replace").splitlines(), 1):
        for name, pattern in PATTERNS:
            for m in pattern.finditer(line):
                value = m.groupdict().get("val") or m.group(0)
                if is_placeholder(value) or is_env_reference(value):
                    continue
                hits.append(Hit(
                    pattern=name,
                    commit="GIT_CONFIG",
                    file=".git/config",
                    line_no=n,
                    masked_value=mask(value),
                    line=redact_line(line.strip()),
                ))
    return hits


# ─────────────────────────────────────────────────────────────────────────────
# Self-verification
# ─────────────────────────────────────────────────────────────────────────────

# A planted secret that MUST be detected. If the scanner misses this, a
# "clean" result from it is meaningless.
CANARY_SECRET = "sk-or-v1-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH"


def verify_scanner_detects_known_secret() -> bool:
    """Prove the pattern set matches a known dummy secret.

    Returns True if the canary is detected in every pattern family it should
    match. Callers must treat a False result as "scanner is broken", not
    "repo is clean".
    """
    checks = [
        ("openrouter_key", CANARY_SECRET),
        ("groq_key", "gsk_AAAABBBBCCCCDDDDEEEEFFFF"),
        ("aws_access_key_id", "AKIAIOSFODNN7EXAMPLE"),
        ("github_token", "ghp_" + "a" * 36),
        ("credential_assignment", 'MT5_PASSWORD = "Sup3rSecretValue123"'),
        # Regression guard: '_' is a word character, so a leading \b on the
        # keyword fails to match inside MT5_PASSWORD. This case must be caught.
        ("credential_assignment", 'OPENROUTER_API_KEY = "abcd1234efgh5678"'),
        ("private_key_block", "-----BEGIN RSA PRIVATE KEY-----"),
    ]
    for name, sample in checks:
        pattern = dict(PATTERNS)[name]
        if not pattern.search(sample):
            print(f"  BROKEN: pattern {name!r} failed to match its own canary",
                  file=sys.stderr)
            return False
        if name == "credential_assignment" and is_placeholder(
                dict(PATTERNS)[name].search(sample).groupdict()["val"]):
            print(f"  BROKEN: {name!r} classified a real secret as a placeholder",
                  file=sys.stderr)
            return False

    # And the placeholder logic must NOT flag documented placeholders.
    for placeholder in ("YOUR_MT5_PASSWORD", "your_password", "changeme"):
        m = dict(PATTERNS)["credential_assignment"].search(
            f'MT5_PASSWORD = "{placeholder}"')
        if m and not is_placeholder(m.groupdict()["val"]):
            print(f"  BROKEN: placeholder {placeholder!r} not recognised",
                  file=sys.stderr)
            return False

    # Env-var reads are correct code, not secrets, and must not be flagged.
    for env_ref in (
        'os.environ.get("MT5_PASSWORD", "")',
        'os.environ["OPENROUTER_API_KEY"]',
        'getattr(config, "OPENROUTER_API_KEY", "")',
        # Passing a variable through is not a secret either.
        "MT5_PASSWORD",
    ):
        m = dict(PATTERNS)["credential_assignment"].search(f"X = {env_ref}")
        if m and not is_env_reference(m.groupdict()["val"]):
            print(f"  BROKEN: env reference {env_ref!r} not recognised",
                  file=sys.stderr)
            return False

    # Truncated example keys and scrub markers must not be reported.
    for not_a_secret in ("sk-or-v1-...", "gsk_...", "<your-key>",
                         "***REMOVED-MT5_PASSWORD***",
                         "[REDACTED]"):
        if not is_placeholder(not_a_secret):
            print(f"  BROKEN: {not_a_secret!r} not recognised as a placeholder",
                  file=sys.stderr)
            return False

    # A real literal must still be flagged even on the same line shape.
    for literal in (
        'MT5_PASSWORD = "Xq7SampleNotRealPwd9z"',
        'OPENROUTER_API_KEY = "sk-or-v1-AAAABBBBCCCCDDDDEEEEFFFF"',
    ):
        m = dict(PATTERNS)["credential_assignment"].search(literal)
        if m is None or is_placeholder(m.groupdict()["val"]) \
                or is_env_reference(m.groupdict()["val"]):
            print(f"  BROKEN: a real literal was suppressed: {literal!r}",
                  file=sys.stderr)
            return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────

def redact_line(line: str, patterns: list[tuple[str, re.Pattern]] | None = None) -> str:
    """Replace any secret-looking substring in a source line with its mask.

    The report shows the offending line for context, so the line must be
    redacted: printing 'MT5_PASSWORD = "Xq7SampleNotRealPwd9z"' into a report file would
    copy the very secret the scan exists to surface.
    """
    patterns = patterns or PATTERNS
    out = line
    for _name, pattern in patterns:
        def _sub(match: re.Match) -> str:
            value = match.groupdict().get("val") or match.group(0)
            return mask(value)
        out = pattern.sub(_sub, out)
    return out


def render_report(history_hits: list[Hit], tree_hits: list[Hit],
                  verified: bool, config_hits: list[Hit] | None = None) -> str:
    config_hits = config_hits or []
    out: list[str] = []
    out.append("=" * 72)
    out.append("  SECRET SCAN REPORT")
    out.append("=" * 72)
    out.append(f"  Scanner self-test : {'PASS' if verified else 'FAIL (results unreliable)'}")
    out.append(f"  History hits      : {len(history_hits)}")
    out.append(f"  Worktree hits     : {len(tree_hits)}")
    out.append(f"  .git/config hits  : {len(config_hits)}")
    out.append("")

    if config_hits:
        out.append("  !! Credentials embedded in .git/config (e.g. a remote URL).")
        out.append("     These are NOT in any commit, so a history rewrite will not")
        out.append("     remove them. Fix the remote URL and rotate the token.")
        out.append("")

    if not verified:
        out.append("  !! Scanner self-test failed; treat all counts below as unreliable.")
        out.append("")

    for label, hits in (("GIT HISTORY", history_hits), ("WORKING TREE", tree_hits),
                        (".GIT/CONFIG", config_hits)):
        out.append(f"--- {label} ---")
        if not hits:
            out.append("  (no hits)")
        else:
            for h in hits:
                date, subject = commit_meta(_REPO_ROOT, h.commit) if h.commit != "WORKTREE" else ("", "")
                when = f" [{date}]" if date else ""
                out.append(f"  {h.pattern}{when}")
                out.append(f"    commit : {h.commit}")
                out.append(f"    file   : {h.file}:{h.line_no}")
                out.append(f"    value  : {h.masked_value}")
                # Redact: the report must not itself become a copy of the secret.
                out.append(f"    line   : {redact_line(h.line)}")
                if subject:
                    out.append(f"    subject: {subject}")
                out.append("")
        out.append("")

    if history_hits or tree_hits:
        out.append("  ACTION REQUIRED: purge these from history with git filter-repo")
    elif config_hits:
        out.append("  ACTION REQUIRED: remove the credential from .git/config and rotate it")
    else:
        out.append("  No secrets found in history, worktree or .git/config.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true",
                        help="run only the scanner self-test")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of text")
    parser.add_argument("--repo", default=str(_REPO_ROOT))
    args = parser.parse_args(argv)

    repo = Path(args.repo)

    verified = verify_scanner_detects_known_secret()
    if args.verify:
        print(f"scanner self-test: {'PASS' if verified else 'FAIL'}")
        return 0 if verified else 1

    history_hits = scan_history(repo)
    tree_hits = scan_tree(repo)
    config_hits = scan_git_config(repo)

    if args.json:
        print(json.dumps({
            "scanner_self_test_passed": verified,
            "history_hits": [h.to_dict() for h in history_hits],
            "worktree_hits": [h.to_dict() for h in tree_hits],
            "git_config_hits": [h.to_dict() for h in config_hits],
        }, indent=2))
    else:
        print(render_report(history_hits, tree_hits, verified, config_hits))

    return 0 if verified else 1


if __name__ == "__main__":
    sys.exit(main())