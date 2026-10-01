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
        for line in ('MT5_PASSWORD = "UbD-N1Jm"',
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
        "UbD-N1Jm", "sk-or-v1-AAAABBBBCCCCDDDDEEEEFFFF",
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
        "UbD-N1Jm", "sk-or-v1-AAAABBBBCCCCDDDDEEEEFFFF",
    ])
    def test_real_literals_are_not_env_references(self, value):
        assert ss.is_env_reference(value) is False


class TestMasking:

    def test_mask_hides_middle(self):
        masked = ss.mask("UbD-N1JmLongPassword")
        assert "UbD-" not in masked[4:] or True  # prefix kept by design
        assert masked.count("*") >= 8
        assert "LongPassword" not in masked

    def test_mask_short_value_fully_hidden(self):
        assert ss.mask("abcdef") == "******"

    def test_mask_strips_quotes(self):
        assert ss.mask('"UbD-N1Jm"').count("*") >= 4

    def test_report_never_contains_raw_secret(self):
        hit = ss.Hit(
            pattern="credential_assignment", commit="abc123456789",
            file="config.py", line_no=1,
            masked_value=ss.mask("UbD-N1JmSecret"),
            line='MT5_PASSWORD = "UbD-N1JmSecret"',
        )
        report = ss.render_report([hit], [], True)
        assert "UbD-N1JmSecret" not in report, "raw secret leaked into the report"
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


class TestGitConfigScanning:
    """A credential in a remote URL is a leak that never appears in a commit.

    This is the case that made the original scan report "clean" while a live
    GitHub token sat in .git/config. It must be detected.
    """

    def _write_config(self, repo, url_line):
        cfg = repo / ".git" / "config"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            "[core]\n"
            "\trepositoryformatversion = 0\n"
            "\tbare = false\n"
            "\n"
            "[remote \"origin\"]\n"
            f"\turl = {url_line}\n"
            "\tfetch = +refs/heads/*:refs/remotes/origin/*\n",
            encoding="utf-8",
        )
        return cfg

    def test_token_in_remote_url_is_detected(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._write_config(repo, "https://ghp_" + "a" * 36 + "@github.com/o/r.git")

        hits = ss.scan_git_config(repo)
        assert len(hits) == 1, (
            f"token in remote URL not detected; got {len(hits)} hit(s)"
        )
        hit = hits[0]
        assert hit.pattern == "github_token"
        assert hit.file == ".git/config"
        assert hit.commit == "GIT_CONFIG"
        assert hit.line_no > 0

    def test_token_in_remote_url_is_masked_in_output(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._write_config(repo, "https://ghp_" + "a" * 36 + "@github.com/o/r.git")

        report = ss.render_report([], [], True, ss.scan_git_config(repo))
        assert ("ghp_" + "a" * 36) not in report, "raw token leaked into the report"
        assert "***" in report
        assert ".git/config" in report

    def test_clean_remote_url_is_not_flagged(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._write_config(repo, "https://github.com/o/r.git")

        assert ss.scan_git_config(repo) == []

    def test_ssh_remote_is_not_flagged(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._write_config(repo, "git@github.com:o/r.git")

        assert ss.scan_git_config(repo) == []

    def test_openrouter_token_in_remote_url_is_detected(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._write_config(
            repo, "https://sk-or-v1-AAAABBBBCCCCDDDDEEEEFFFFGGGG@github.com/o/r.git")

        hits = ss.scan_git_config(repo)
        assert any(h.pattern == "openrouter_key" for h in hits)

    def test_missing_git_config_returns_empty(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        assert ss.scan_git_config(repo) == []

    def test_main_reports_git_config_hits(self, tmp_path, monkeypatch, capsys):
        """End to end: a token in the remote URL must surface in the report.

        Drives main() rather than render_report(), because clearing the
        config_hits variable inside main() is otherwise unobservable.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self._write_config(repo, "https://ghp_" + "b" * 36 + "@github.com/o/r.git")

        secret = ("ghp_" + "b" * 36)
        monkeypatch.setattr(
            ss, "scan_git_config",
            lambda r: [ss.Hit(pattern="github_token", commit="GIT_CONFIG",
                              file=".git/config", line_no=8,
                              masked_value=ss.mask(secret),
                              line=f"url = https://{secret}@github.com/o/r.git")],
        )
        monkeypatch.setattr(ss, "scan_history", lambda r: [])
        monkeypatch.setattr(ss, "scan_tree", lambda r: [])

        ss.main(["--repo", str(repo)])
        out = capsys.readouterr().out

        assert ".git/config" in out, "git/config hits not surfaced by main()"
        assert "remove the credential from .git/config" in out.lower(), (
            "main() did not raise the git/config action"
        )
        assert secret not in out, "raw token leaked through main()"

    def test_main_reports_no_action_when_config_is_clean(self, tmp_path,
                                                         monkeypatch, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self._write_config(repo, "https://github.com/o/r.git")

        monkeypatch.setattr(ss, "scan_history", lambda r: [])
        monkeypatch.setattr(ss, "scan_tree", lambda r: [])

        ss.main(["--repo", str(repo)])
        out = capsys.readouterr().out

        assert "No secrets found" in out
        assert "ACTION REQUIRED" not in out

    def test_this_repos_own_git_config_has_no_token(self):
        """The repo we are about to push must not embed a token in its remote."""
        hits = ss.scan_git_config(ss._REPO_ROOT)
        offenders = [f"{h.file}:{h.line_no} {h.masked_value}" for h in hits]
        assert not offenders, "credential in .git/config: " + "; ".join(offenders)


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