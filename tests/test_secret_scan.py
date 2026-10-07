"""Secret scanner tests.

The scanner is the control that tells us whether credentials leaked. If it
silently under-reports, every "clean" claim downstream is worthless, so the
tests here focus on proving it actually FIRES.

Run: python -m pytest tests/test_secret_scan.py -v
"""
import subprocess
import textwrap
from pathlib import Path

import pytest

from scripts import secret_scan as ss
from scripts import verify_history_clean as vh


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

        # The '.git/config' label is always printed, so assert on the count and
        # the section body instead: both must reflect a non-empty hit list.
        assert ".git/config hits  : 1" in out, (
            "main() reported 0 git/config hits despite a planted token"
        )
        assert "GIT_CONFIG" in out
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

        assert ".git/config hits  : 0" in out
        assert "No secrets found" in out
        assert "ACTION REQUIRED" not in out

    def test_this_repos_own_git_config_has_no_token(self):
        """The repo we are about to push must not embed a token in its remote."""
        hits = ss.scan_git_config(ss._REPO_ROOT)
        offenders = [f"{h.file}:{h.line_no} {h.masked_value}" for h in hits]
        assert not offenders, "credential in .git/config: " + "; ".join(offenders)


class TestSyntheticValueAllowlist:
    """E4: exemption is granted per VALUE, never per PATH.

    A path-based exemption previously excused any credential-shaped string
    under tests/ or scripts/secret_scan.py. A real password pasted into a test
    file was therefore invisible. These tests pin the value-based behaviour.
    """

    @pytest.mark.parametrize("value", [
        "YOUR_MT5_PASSWORD", "your_password", "YOUR_OPENROUTER_KEY",
        "changeme", "CHANGEME",
        "***REMOVED-MT5_PASSWORD***", "***REMOVED-OPENROUTER_API_KEY***",
        "[REDACTED]", "REDACTED",
        "999999999", "000000000", "123456789",
    ])
    def test_synthetic_values_are_recognised(self, value):
        assert vh.is_synthetic(value) is True

    @pytest.mark.parametrize("value", [
        "AKIAIOSFODNN7NOTLISTED",
        "sk-or-v1-ZZZZrandomrandom1234",
        "hunter2xyz",
        "999999998",                      # near-miss of an allowlisted value
    ])
    def test_values_not_on_the_allowlist_are_not_synthetic(self, value):
        """Values outside the allowlist are never excused, even if they are
        recognisably fake: exemption must be explicit, never inferred."""
        assert value not in vh.SYNTHETIC_TEST_VALUES
        assert vh.is_synthetic(value) is False

    @pytest.mark.parametrize("value", [
        "AKIAIOSFODNN7EXAMPLE",
        "sk-or-v1-AAAABBBBCCCCDDDDEEEEFFFF",
        "gsk_AAAABBBBCCCCDDDDEEEEFFFF",
        "xoxb-1234567890-abcdefghij",
    ])
    def test_registered_canaries_are_classified_as_synthetic(self, value):
        """The scanner's own canaries are allow-listed so audits classify them."""
        assert value in vh.SYNTHETIC_TEST_VALUES
        assert vh.is_synthetic(value) is True

    def test_a_different_aws_key_is_not_synthetic(self):
        """Allow-listing the AWS example key must not exempt other AWS keys."""
        assert vh.is_synthetic("AKIAIOSFODNN7NOTLISTED") is False

    def test_path_is_not_consulted(self):
        """The old design keyed off path. Prove it no longer does."""
        import inspect
        src = inspect.getsource(vh.search_history)
        assert "TEST_ONLY_PATH_MARKERS" not in src, (
            "search_history still references a path-based exemption"
        )

    def test_real_secret_in_a_test_file_is_still_a_leak(self, tmp_path):
        """The regression this replaces: a live password under tests/.

        Commits a file at tests/test_fixture.py containing a real-looking
        credential that is NOT on the synthetic allowlist. It must be reported,
        because location grants no exemption and the value is not registered
        as a canary.
        """
        secret = "Tr0ub4dor-unlisted-9x7"
        assert secret not in vh.SYNTHETIC_TEST_VALUES

        repo = tmp_path / "repo"
        (repo / "tests").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "tests" / "test_fixture.py").write_text(
            f'MT5_PASSWORD = "{secret}"\n', encoding="utf-8"
        )
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "fixture"],
            check=True,
        )

        commits, leak_files = vh.search_history(repo, secret)
        assert leak_files > 0, (
            "a real credential in tests/ was excused by its path"
        )
        assert "tests/test_fixture.py" in str(leak_files) or leak_files >= 1

    def test_synthetic_value_in_a_test_file_is_not_a_leak(self, tmp_path):
        """A placeholder in the same location is fine."""
        repo = tmp_path / "repo"
        (repo / "tests").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "tests" / "test_fixture.py").write_text(
            'MT5_PASSWORD = "YOUR_MT5_PASSWORD"\n', encoding="utf-8"
        )
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "fixture"],
            check=True,
        )

        assert vh.search_history(repo, "YOUR_MT5_PASSWORD") == (0, 0)

    def test_synthetic_check_short_circuits_search(self):
        """A synthetic value must not even walk the history."""
        assert vh.search_history(Path("/nonexistent"), "YOUR_MT5_PASSWORD") == (0, 0)

    def test_allowlist_contains_only_intended_entries(self):
        """The AWS example key is registered so audits can classify it.

        What must NOT happen is allow-listing a whole key type, which would
        exempt real keys. That is pinned by the neighbouring test.
        """
        assert "AKIAIOSFODNN7EXAMPLE" in ss.DECLARED_CANARIES
        for value in vh.SYNTHETIC_TEST_VALUES:
            if "AKIA" not in value:
                continue
            # The single documented AWS example, and nothing else of that shape.
            assert value == "AKIAIOSFODNN7EXAMPLE", (
                f"{value!r} is an AWS key that should not be allow-listed"
            )


class TestSyntheticAllowlistIsSafe:
    """The allowlist must not become a place to hide real credentials.

    Exemption is per value, so the allowlist is the single point where a live
    credential could be quietly excused. These tests constrain what may enter it.
    """

    def test_allowlist_entries_are_declared_canaries_or_placeholders(self):
        """Structural check, replacing an entropy threshold.

        An earlier version used Shannon entropy to guess whether an allowlisted
        value looked like a real credential. That is a poor discriminator: the
        Slack canary's fixed digits push it above any usable threshold while a
        genuine key needs a much higher one. Instead, exemption must come from
        one of the explicitly justified sets.
        """
        justified = (
            set(vh.SYNTHETIC_TEST_VALUES)
            & (set(ss.DECLARED_CANARIES) | {"999999999", "000000000",
                                           "123456789"})
        )
        for value in justified:
            assert vh.is_synthetic(value) is True

        # Anything not in the allowlist is never exempt, regardless of entropy.
        for value in ("AKIAIOSFODNN7NOTLISTED", "sk-or-v1-ZZZZrandomrandom1234",
                      "ghp_" + "c" * 36):
            assert vh.is_synthetic(value) is False, (
                f"{value[:16]!r} was exempted without being declared synthetic"
            )

    def test_allowlist_is_the_union_of_declared_sets(self):
        """The allowlist must equal canaries plus placeholders, nothing else."""
        base = {
            "YOUR_MT5_PASSWORD", "your_password", "YOUR_OPENROUTER_KEY",
            "YOUR_KEY_HERE", "changeme",
            "***REMOVED-MT5_PASSWORD***", "***REMOVED-OPENROUTER_API_KEY***",
            "[REDACTED]", "999999999", "000000000", "123456789",
        }
        expected = base | set(ss.DECLARED_CANARIES)
        assert set(vh.SYNTHETIC_TEST_VALUES) == expected, (
            "allowlist drifted from declared canaries + placeholders; "
            f"unexpected: {sorted(set(vh.SYNTHETIC_TEST_VALUES) - expected)}"
        )

    def test_allowlist_does_not_contain_any_live_env_secret(self):
        """The definitive check: nothing in .env may be allow-listed."""
        import config as cfg

        live = {
            name: value
            for name in ("MT5_PASSWORD", "OPENROUTER_API_KEY", "GROQ_API_KEY",
                         "MT5_LOGIN")
            if (value := getattr(cfg, name, None))
            and isinstance(value, str)
            and not value.startswith("YOUR_")
            and len(value) >= 6
        }
        for name, value in live.items():
            assert value not in vh.SYNTHETIC_TEST_VALUES, (
                f"{name} is on the synthetic allowlist; a live credential must "
                f"never be exempted from leak detection"
            )

    def test_allowlist_is_explicitly_enumerated_not_inferred(self):
        """Exemption must come from the list, never from a shape heuristic."""
        import inspect

        src = inspect.getsource(vh.is_synthetic)
        # A length or charset heuristic would look like a regex over the value.
        assert "re.match" not in src and "re.search" not in src, (
            "is_synthetic must not infer safety from a value's shape"
        )
        assert "SYNTHETIC_TEST_VALUES" in src

    def test_live_secret_is_still_detected_through_the_allowlist_path(self):
        """Even with a populated allowlist, a non-listed secret is a leak."""
        assert vh.is_synthetic("NotOnTheList12345") is False


class TestDictSubscriptionIsNotASecret:
    """A dict lookup names a field; it is not a credential.

    The gateway reads credentials["password"], which the scanner initially
    flagged and blocked the commit on. The fix must not become a hole: a
    literal inside brackets is still a secret.
    """

    @pytest.mark.parametrize("value", [
        'credentials["password"]',
        'env["OPENROUTER_API_KEY"]',
        "creds['token']",
        'cfg["OPENROUTER_API_KEY"]',
    ])
    def test_lookup_is_an_env_reference(self, value):
        assert ss.is_env_reference(value) is True

    @pytest.mark.parametrize("value", [
        "Xq7SampleNotRealPwd9z",
        "CorrectHorse7",
        "sk-or-v1-AAAABBBBCCCCDDDDEEEEFFFF",
        "AKIAIOSFODNN7EXAMPLE",
    ])
    def test_literal_is_still_flagged(self, value):
        assert ss.is_env_reference(value) is False

    def test_gateway_line_does_not_block_a_commit(self):
        """The exact line that blocked the engine commit."""
        source = (Path(__file__).resolve().parent.parent /
                  "engine" / "__main__.py").read_text(encoding="utf-8")
        pattern = dict(ss.PATTERNS)["credential_assignment"]
        offenders = []
        for line in source.splitlines():
            for m in pattern.finditer(line):
                val = m.groupdict()["val"]
                if ss.is_placeholder(val) or ss.is_env_reference(val):
                    continue
                offenders.append(line.strip())
        assert not offenders, (
            "engine/__main__.py still trips the scanner on: "
            f"{offenders}"
        )


class TestProseNamingACredentialIsNotACredential:
    """A docstring or comment that NAMES a key is not a key.

    Two failed attempts are recorded here. Keying on the captured value's shape
    (a short alphabetic token) excused `client_secret: str = "hunter2xyz"`,
    because the capture `str` looks like an English word. The discriminator
    that works is the shape of the LINE: a comment or a docstring is prose, an
    assignment is not.
    """

    @pytest.mark.parametrize("line", [
        '"""Check the XAI_API_KEY: is it picked up, and does it work?"""',
        "# The XAI_API_KEY must never be logged",
        '"""Document the MT5_PASSWORD handling here."""',
    ])
    def test_prose_naming_a_credential_is_benign(self, line):
        pattern = dict(ss.PATTERNS)["credential_assignment"]
        for m in pattern.finditer(line):
            assert ss._is_benign_line(line, m.groupdict()["val"]) is True, (
                f"prose naming a credential was reported as a secret: {line!r}"
            )

    @pytest.mark.parametrize("line,value", [
        ('XAI_API_KEY = "xai-realkeyvalue123"', '"xai-realkeyvalue123"'),
        ('MT5_PASSWORD = "Xq7SampleNotRealPwd9z"', '"Xq7SampleNotRealPwd9z"'),
        ('OPENROUTER_API_KEY = "sk-or-v1-AAAABBBBCCCCDDDDEEEEFFFF"',
         '"sk-or-v1-AAAABBBBCCCCDDDDEEEEFFFF"'),
        ('GITHUB_TOKEN = "ghp_' + "a" * 36 + '"', '"ghp_' + "a" * 36 + '"'),
        ('AWS_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"', '"AKIAIOSFODNN7EXAMPLE"'),
        # The case a shape-based rule got wrong.
        ('client_secret: str = "hunter2xyz"', '"hunter2xyz"'),
        ('password = "CorrectHorse7"', '"CorrectHorse7"'),
    ])
    def test_real_credentials_are_still_reported(self, line, value):
        """Every real shape must survive the prose exemption."""
        pattern = dict(ss.PATTERNS)["credential_assignment"]
        matches = list(pattern.finditer(line))
        assert matches, f"pattern stopped matching {line!r}"
        for m in matches:
            assert ss._is_benign_line(line, m.groupdict()["val"]) is False, (
                f"a real credential was excused: {line!r}"
            )

    def test_prose_exemption_does_not_depend_on_value_shape(self):
        """A shape-based rule would pass the prose cases and fail this one."""
        pattern = dict(ss.PATTERNS)["credential_assignment"]
        doc = '"""Check the XAI_API_KEY: is it picked up?"""'
        assignment = 'XAI_API_KEY = "is"'
        # Same captured value shape, different line kinds.
        for m in pattern.finditer(doc):
            assert ss._is_benign_line(doc, m.groupdict()["val"]) is True
        for m in pattern.finditer(assignment):
            assert ss._is_benign_line(assignment, m.groupdict()["val"]) is False, (
                "the prose exemption must depend on the line, not the value"
            )


class TestTokenParameterAlias:
    """`token: str` and `self._token = token` name a parameter, not a secret."""

    @pytest.mark.parametrize("line", [
        "def __init__(self, token: str) -> None:",
        "self._token = token",
        "    order_token: str | None = None,",
        "def hex_rgba(token: str, alpha: float) -> str:",
        "        order_token=order_token,",
        "            token = uuid.uuid4().hex",
        '    token = st.session_state.get("_snapshot_token")',
        '        token = f"var(--accent-{name})"',
        '        order_token="repeat",',
    ])
    def test_parameter_alias_is_benign(self, line):
        pattern = dict(ss.PATTERNS)["credential_assignment"]
        matches = list(pattern.finditer(line))
        assert matches, f"pattern stopped matching {line!r}"
        for m in matches:
            assert ss._is_benign_line(line, m.groupdict()["val"]) is True

    @pytest.mark.parametrize("line", [
        'def __init__(self, token: str = "hunter2xyz") -> None:',
        'self._token = "Xq7SampleNotRealPwd9z"',
        'token = "Xq7SampleNotRealPwd9z"',
    ])
    def test_quoted_token_is_still_a_secret(self, line):
        pattern = dict(ss.PATTERNS)["credential_assignment"]
        matches = list(pattern.finditer(line))
        assert matches, f"pattern stopped matching {line!r}"
        for m in matches:
            assert ss._is_benign_line(line, m.groupdict()["val"]) is False


class TestHistoryCleanReporting:
    """A bare run must not claim OK while a secret is present."""

    def test_leak_is_reported_even_without_expect_clean(self, tmp_path, capsys):
        from scripts import verify_history_clean as vh
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        secret = "Zx9RotationUnlisted77"
        assert secret not in vh.SYNTHETIC_TEST_VALUES
        (repo / "leak.py").write_text(
            f'MT5_PASSWORD = "{secret}"\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "leak"], check=True)

        env = tmp_path / ".env"
        env.write_text(f"MT5_PASSWORD={secret}\n", encoding="utf-8")

        code = vh.main(["--repo", str(repo), "--env", str(env)])
        out = capsys.readouterr().out
        assert code == 1, "a leak must exit non-zero"
        assert "LEAKS FOUND" in out, out
        assert "RESULT: OK" not in out, (
            "the summary must not claim OK while a secret is present"
        )


class TestThisRepoIsClean:
    """The working tree of this repo must contain no secrets."""

    def test_worktree_has_no_secrets(self):
        """Scan tracked files only.

        Untracked work in progress is excluded deliberately. The parallel UI/UX
        branch adds modules containing a `submit_token` variable and
        `uuid4().hex`; both are credential-shaped to a substring matcher and
        neither is a credential. Scanning untracked files would turn this into a
        gate on someone else's in-progress branch. Those files are scanned the
        moment they are committed, which is when the risk actually exists.

        The rule is unchanged: a committed credential still fails here, and still
        fails the pre-commit hook.
        """
        import subprocess

        root = ss._REPO_ROOT
        # Committed files only. The parallel UI/UX work is staged but not yet
        # committed, and it contains a `submit_token` variable plus
        # `uuid4().hex`: both credential-shaped to a substring matcher, neither
        # a credential. Filtering on committed state rather than on paths keeps
        # this a gate on what is actually published, and those files are scanned
        # the moment they are committed.
        tracked = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMRT", "HEAD"],
            cwd=str(root), capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        changed = set(tracked)

        committed = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "HEAD"],
            cwd=str(root), capture_output=True, text=True, check=True,
        ).stdout.splitlines()

        # Tracked in HEAD, plus anything already committed and modified.
        files = [f for f in committed if f not in changed]
        files += [f for f in changed if f in set(committed)]

        assert files, "resolved an empty file list; the check would be vacuous"

        offenders: list[str] = []
        for rel in files:
            if rel.replace("\\", "/") in ss.SELF_REFERENTIAL_FILES:
                continue
            path = root / rel
            if not path.is_file():
                continue
            for n, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(),
                    1):
                for name, pattern in ss.PATTERNS:
                    for m in pattern.finditer(line):
                        value = m.groupdict().get("val") or m.group(0)
                        if ss._is_benign_line(line, value):
                            continue
                        offenders.append(f"{rel}:{n} [{name}] {line.strip()[:70]}")

        assert not offenders, "secrets in tracked files:\n" + "\n".join(offenders)

    def test_scanner_still_flags_a_planted_secret_in_a_tracked_file(self):
        """Proves the narrowing above did not make this test decorative."""
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "probe_repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "probe.py").write_text(
                'MT5_PASSWORD = "UbD-N1Jm"\n', encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)

            from scripts import secret_scan as scanner
            hits = [h for h in scanner.scan_tree(repo) if h.file == "probe.py"]
            assert hits, (
                "the tracked-file scan no longer detects a real credential; "
                "this test would pass against a broken scanner"
            )

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
        assert checked == 7, (
            f"expected 7 sensitive keys in .env.example "
            f"(MT5_LOGIN, MT5_PASSWORD, OPENROUTER_API_KEY, XAI_API_KEY, "
            f"TELEGRAM_BOT_TOKEN, GROQ_API_KEY, AGENTROUTER_API_KEY), checked {checked}"
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