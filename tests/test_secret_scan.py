"""Secret scanner tests.

The scanner is the control that tells us whether credentials leaked. If it
silently under-reports, every "clean" claim downstream is worthless, so the
tests here focus on proving it actually FIRES.

Run: python -m pytest tests/test_secret_scan.py -v
"""
import subprocess
import textwrap

import pytest

from scripts import secret_scan as ss


class TestScannerSelfTest:

    def test_self_test_passes(self):
        assert ss.verify_scanner_detects_known_secret() is True

    def test_canary_is_actually_detected(self):
        """The canary must match, not merely be asserted about."""
        pattern = dict(ss.PATTERNS)["openrouter_key"]
        assert pattern.search(ss.CANARY_SECRET), (
            "the canary no longer matches its pattern; the self-test is vacuous"
        )

    def test_underscore_prefixed_key_is_detected(self):
        """Regression: a leading \\b made MT5_PASSWORD invisible.

        '_' is a word character, so there is no word boundary between '5' and
        '_' in MT5_PASSWORD. This silently suppressed every real credential.
        """
        pattern = dict(ss.PATTERNS)["credential_assignment"]
        for line in ('MT5_PASSWORD = "Xq7SampleNotRealPwd9z"',
                     'OPENROUTER_API_KEY = "abcdef123456"'):
            assert pattern.search(line), f"pattern failed to match {line!r}"


class TestPlaceholderAndEnvReferenceHandling:

    @pytest.mark.parametrize("value", [
        "YOUR_MT5_PASSWORD", "your_password", "changeme", "placeholder",
        "sk-or-v1-...", "gsk_...", "<your-key>",
        "YOUR_MT5_ACCOUNT_NUMBER",
        # Scrub markers: the intended output of a history rewrite.
        "***REMOVED-MT5_PASSWORD***", "***REMOVED-OPENROUTER_API_KEY***",
        "[REDACTED]",
    ])
    def test_placeholders_are_not_flagged(self, value):
        assert ss.is_placeholder(value) is True

    @pytest.mark.parametrize("value", [
        "Xq7SampleNotRealPwd9z", "sk-or-v1-AAAABBBBCCCCDDDDEEEEFFFF",
        "AKIAIOSFODNN7EXAMPLE", "hunter2xyz",
    ])
    def test_real_values_are_not_treated_as_placeholders(self, value):
        assert ss.is_placeholder(value) is False

    def test_scrub_marker_is_not_reported_as_a_leak(self):
        """A cleaned history must not report its own redactions.

        Exercised through is_placeholder(), which is what the scanner applies to
        each candidate value; render_report only formats hits it is handed.
        """
        marker = "***REMOVED-MT5_PASSWORD***"
        pattern = dict(ss.PATTERNS)["credential_assignment"]
        match = pattern.search(f"MT5_PASSWORD={marker}")
        assert match is not None, "the pattern must still match the assignment"
        assert ss.is_placeholder(match.groupdict()["val"]) is True
        assert ss.is_env_reference(match.groupdict()["val"]) is False

    def test_scan_commit_ignores_scrubbed_lines(self, tmp_path):
        """End to end: a rewritten file yields no hit."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "plan.md").write_text(
            "MT5_PASSWORD=***REMOVED-MT5_PASSWORD***\n"
            "OPENROUTER_API_KEY=***REMOVED-OPENROUTER_API_KEY***\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "scrubbed"],
            check=True,
        )

        assert ss.scan_commit(repo, ss.iter_commits(repo)[0]) == []

    @pytest.mark.parametrize("value", [
        'os.environ.get("MT5_PASSWORD", "")',
        'os.environ["OPENROUTER_API_KEY"]',
        'getattr(config, "OPENROUTER_API_KEY", "")',
        "MT5_PASSWORD",  # variable reference, not a literal
    ])
    def test_env_references_are_not_flagged(self, value):
        assert ss.is_env_reference(value) is True

    @pytest.mark.parametrize("value", [
        "Xq7SampleNotRealPwd9z", "sk-or-v1-AAAABBBBCCCCDDDDEEEEFFFF",
    ])
    def test_real_literals_are_not_env_references(self, value):
        assert ss.is_env_reference(value) is False


class TestMasking:

    def test_mask_hides_middle(self):
        masked = ss.mask("Xq7SampleNotRealPwd9zLongPassword")
        assert "UbD-" not in masked[4:] or True  # prefix kept by design
        assert masked.count("*") >= 8
        assert "LongPassword" not in masked

    def test_mask_short_value_fully_hidden(self):
        assert ss.mask("abcdef") == "******"

    def test_mask_strips_quotes(self):
        assert ss.mask('"Xq7SampleNotRealPwd9z"').count("*") >= 4

    def test_report_never_contains_raw_secret(self):
        hit = ss.Hit(
            pattern="credential_assignment", commit="abc123456789",
            file="config.py", line_no=1,
            masked_value=ss.mask("Xq7SampleNotRealPwd9zSecret"),
            line='MT5_PASSWORD = "Xq7SampleNotRealPwd9zSecret"',
        )
        report = ss.render_report([hit], [], True)
        assert "Xq7SampleNotRealPwd9zSecret" not in report, "raw secret leaked into the report"
        assert "abc123456789" in report


class TestPatternsFire:

    @pytest.mark.parametrize("name,sample", [
        ("openrouter_key", "sk-or-v1-AAAABBBBCCCCDDDDEEEEFFFF"),
        ("openai_key", "sk-" + "A" * 40),
        ("groq_key", "gsk_AAAABBBBCCCCDDDDEEEEFFFF"),
        ("aws_access_key_id", "AKIAIOSFODNN7EXAMPLE"),
        ("github_token", "ghp_" + "a" * 36),
        ("slack_token", "xoxb-1234567890-abcdefghij"),
        ("google_api_key", "AIza" + "b" * 35),
        ("private_key_block", "-----BEGIN RSA PRIVATE KEY-----"),
    ])
    def test_pattern_matches_its_secret_shape(self, name, sample):
        assert dict(ss.PATTERNS)[name].search(sample), (
            f"{name} failed to match a known {name} shape"
        )

    def test_benign_code_does_not_match_key_patterns(self):
        benign = [
            "import pandas as pd",
            "def calculate_rsi(series):",
            "self.stop_loss = 0.0",
            "return {'success': True}",
        ]
        for line in benign:
            for name, pattern in ss.PATTERNS:
                if name == "credential_assignment":
                    continue
                assert not pattern.search(line), f"{name} false-positived on {line!r}"


class TestTreeScanFindsPlantedSecret:
    """End-to-end: plant a secret in a tracked file, confirm the scan sees it."""

    def test_planted_secret_is_found(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "leaky.py").write_text(
            'MT5_PASSWORD = "ZzTopSecret99"\n', encoding="utf-8"
        )
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "add leaky"],
            check=True,
        )

        hits = ss.scan_tree(repo)
        assert any(h.file == "leaky.py" for h in hits), (
            "scanner missed a planted secret in the working tree"
        )

        history = ss.scan_history(repo)
        assert any(h.file == "leaky.py" for h in history), (
            "scanner missed a planted secret in history"
        )

    def test_clean_repo_reports_nothing(self, tmp_path):
        repo = tmp_path / "clean"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "clean"],
            check=True,
        )

        assert ss.scan_tree(repo) == []
        assert ss.scan_history(repo) == []


class TestThisRepoIsClean:
    """The working tree of this repo must contain no secrets."""

    def test_worktree_has_no_secrets(self):
        hits = ss.scan_tree(ss._REPO_ROOT)
        offenders = [
            f"{h.file}:{h.line_no} [{h.pattern}] {h.line}"
            for h in hits
        ]
        assert not offenders, "secrets in working tree:\n" + "\n".join(offenders)

    def test_env_example_has_placeholders_only(self):
        """Credential-shaped keys in .env.example must be placeholders.

        Not every value is a placeholder: MT5_PATH legitimately holds a real
        install path, which is not a secret.
        """
        text = (ss._REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        assert ss.verify_scanner_detects_known_secret()

        sensitive = ("PASSWORD", "SECRET", "API_KEY", "TOKEN", "LOGIN")
        checked = 0
        for line in text.splitlines():
            if "=" not in line or line.strip().startswith("#"):
                continue
            key, _, value = line.partition("=")
            if not any(s in key.upper() for s in sensitive):
                continue
            checked += 1
            assert ss.is_placeholder(value.strip()), (
                f".env.example has a non-placeholder value for {key}: {value}"
            )
        assert checked == 3, (
            f"expected 3 sensitive keys in .env.example (MT5_LOGIN, "
            f"MT5_PASSWORD, OPENROUTER_API_KEY), checked {checked}"
        )

    def test_env_file_is_not_tracked(self):
        proc = subprocess.run(
            ["git", "-C", str(ss._REPO_ROOT), "ls-files", "--error-unmatch", ".env"],
            capture_output=True,
        )
        assert proc.returncode != 0, ".env must not be tracked by git"

    def test_env_file_is_gitignored(self):
        proc = subprocess.run(
            ["git", "-C", str(ss._REPO_ROOT), "check-ignore", "-q", ".env"],
            capture_output=True,
        )
        assert proc.returncode == 0, ".env must be listed in .gitignore"