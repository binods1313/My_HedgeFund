"""Classifier membership. The match is never written into the assertion text."""

from __future__ import annotations

from scripts.classify_scan_hits import classify, format_row, match_hash
from scripts.secret_scan import DECLARED_CANARIES


def test_known_canary_requires_file_and_allowlist() -> None:
    sample = sorted(DECLARED_CANARIES)[0]
    assert classify("scripts/secret_scan.py", sample) == "KNOWN_CANARY"
    assert classify("tests\\test_secret_scan.py", sample) == "KNOWN_CANARY"
    assert classify("scripts/precommit_hook.py", sample) == "UNKNOWN"
    assert classify("tests/test_engine/test_boundaries.py", sample) == "UNKNOWN"
    assert classify("scripts/secret_scan.py", sample + "-extra") == "UNKNOWN"
    row = format_row("scripts/secret_scan.py", "abc123456789", "credential_assignment", sample)
    assert sample not in row
    assert match_hash(sample) in row
    assert row.endswith("\tKNOWN_CANARY")
    unknown = format_row("engine/config.py", "abc123456789", "credential_assignment", sample)
    assert sample not in unknown
    assert unknown.endswith("\tUNKNOWN")
