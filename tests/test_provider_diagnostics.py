"""Failure classes come from documented status codes. No network."""

from __future__ import annotations

from scripts.check_agentrouter import error_code, failure_class
from ui.services.infoway_client import infoway_error_code, infoway_failure_class


def test_agentrouter_maps_only_documented_statuses() -> None:
    assert failure_class(key_present=False, http_status="N/A") == "missing key"
    assert failure_class(key_present=True, http_status="N/A") == "network"
    assert failure_class(key_present=True, http_status=401) == "auth rejected"
    assert failure_class(key_present=True, http_status=404) == "wrong endpoint"
    assert failure_class(key_present=True, http_status=200) == "none"
    assert failure_class(key_present=True, http_status=429) == "unclassified"
    assert failure_class(key_present=True, http_status=403) == "unclassified"
    assert failure_class(key_present=True, http_status=503) == "unclassified"
    assert error_code(401) == "401"
    assert error_code(200) == "none"
    assert error_code("N/A") == "none"


def test_infoway_maps_the_published_rest_table() -> None:
    assert infoway_failure_class(
        configured=False, http_status=None, ret=None, transport_failed=False, ok=False
    ) == "missing key"
    assert infoway_failure_class(
        configured=True, http_status=401, ret=None, transport_failed=False, ok=False
    ) == "auth rejected"
    assert infoway_failure_class(
        configured=True, http_status=404, ret=None, transport_failed=False, ok=False
    ) == "wrong endpoint"
    assert infoway_failure_class(
        configured=True, http_status=429, ret=None, transport_failed=False, ok=False
    ) == "rate or plan limit"
    assert infoway_failure_class(
        configured=True, http_status=524, ret=None, transport_failed=False, ok=False
    ) == "network"
    assert infoway_failure_class(
        configured=True, http_status=499, ret=None, transport_failed=False, ok=False
    ) == "unclassified"
    assert infoway_failure_class(
        configured=True, http_status=200, ret=501, transport_failed=False, ok=False
    ) == "rate or plan limit"
    assert infoway_failure_class(
        configured=True, http_status=200, ret=500, transport_failed=False, ok=False
    ) == "unclassified"
    assert infoway_failure_class(
        configured=True, http_status=200, ret=509, transport_failed=False, ok=False
    ) == "auth rejected"
    assert infoway_failure_class(
        configured=True, http_status=None, ret=None, transport_failed=True, ok=False
    ) == "network"
    assert infoway_failure_class(
        configured=True, http_status=None, ret=None, transport_failed=False, ok=False
    ) == "unclassified"
    assert infoway_failure_class(
        configured=True, http_status=200, ret=200, transport_failed=False, ok=True
    ) == "none"
    assert infoway_error_code(http_status=200, ret=501) == "501"
    assert infoway_error_code(http_status=401, ret=None) == "401"
    assert infoway_error_code(http_status=200, ret=200) == "none"
