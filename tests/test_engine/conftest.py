"""Shared fixtures for the engine test package.

Every test in this package that calls `build_engine` receives an `MT5_LOGIN`
matching FakeMT5's own account. The account guard compares the configured
login against the terminal's, which is exactly right in production and exactly
wrong in a test whose subject is telemetry or reconcile. Making the credential
match is done here, once, rather than in each test, so a test's failure cannot
be an unrelated mismatch and a new test cannot silently start failing when the
guard is tightened.

No real terminal is involved: the fake is injected via `mt5_module=`.
"""
from __future__ import annotations

import pytest

from tests.test_engine.fake_mt5 import matching_fake_login


@pytest.fixture(autouse=True)
def _match_fake_account(monkeypatch):
    """Configure MT5_LOGIN / MT5_SERVER to match FakeMT5's own account."""
    matching_fake_login(monkeypatch)
    yield
