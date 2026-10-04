r"""Tests for the xAI provider client.

The important properties are failure-mode honesty and redaction:

  - Every failure raises a typed Provider* error. It never returns a
    Validation, because a caller treating an exception as "the model declined"
    would conflate a broken provider with a considered opinion.
  - Timeouts do not retry. A late retry can land after the decision window.
  - The key cannot appear in repr, str, or any error message.
  - The snapshot carries numbers only, so a cited value outside it is provably
    invented.

Run: python -m pytest tests/test_engine/test_llm_client.py -v
"""
import json

import pytest

from engine.keys import Keyring
from engine.llm_client import (
    ProviderNotConfigured,
    ProviderError,
    ProviderResponseInvalid,
    ProviderTimeout,
    ProviderUnavailable,
    Snapshot,
    Validation,
    Verdict,
    XAIClient,
)
from scripts.secret_scan import DECLARED_CANARIES

# A registered synthetic canary, imported rather than written out. Spelling the
# value here would put a credential-shaped literal in a tracked file for no
# reason; importing it keeps the secret scanner's job meaningful and this module
# free of anything that looks like a key.
FAKE_KEY = next(c for c in DECLARED_CANARIES if c.startswith("sk-or-v1-"))


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def ok_response(**overrides):
    body = {
        "choices": [{"message": {"content": json.dumps({
            "verdict": "APPROVE",
            "confidence_adjust": 0,
            "rationale": "EMA20 above EMA50 at 1.0990.",
            "cited_values": {"ema20": 1.0990},
            **overrides,
        })}}],
    }
    return FakeResponse(body)


def make_client(response=None, env=None, **kwargs):
    environment = env if env is not None else {"XAI_API_KEY": FAKE_KEY}
    ring = Keyring(env=environment)
    calls = {}

    def post(url, *, headers, json, timeout):
        calls["url"] = url
        calls["headers"] = headers
        calls["json"] = json
        calls["timeout"] = timeout
        if isinstance(response, Exception):
            raise response
        return response

    client = XAIClient(ring, http_post=post, **kwargs)
    return client, calls


SNAPSHOT = Snapshot(
    symbol="EURUSD", timeframe="M15", price=1.10000,
    indicators={"ema20": 1.09900, "rsi": 55.0},
    context={"atr": 0.00040},
)


# ─────────────────────────────────────────────────────────────────────────────
# Redaction
# ─────────────────────────────────────────────────────────────────────────────

class TestKeyRedaction:

    def test_repr_hides_the_key(self):
        client, _ = make_client()
        assert FAKE_KEY not in repr(client)
        assert "REDACTED" in repr(client)

    def test_str_hides_the_key(self):
        client, _ = make_client()
        assert FAKE_KEY not in str(client)

    def test_describe_hides_the_key(self):
        client, _ = make_client()
        assert FAKE_KEY not in client.describe()

    def test_missing_key_error_names_the_variable_not_the_value(self):
        client, _ = make_client(env={})
        with pytest.raises(ProviderNotConfigured) as excinfo:
            client.validate_trade(SNAPSHOT)
        assert "XAI_API_KEY" in str(excinfo.value)

    def test_placeholder_key_is_not_configured(self):
        client, _ = make_client(env={"XAI_API_KEY": "YOUR_XAI_KEY"})
        assert client.is_configured() is False
        with pytest.raises(ProviderNotConfigured):
            client.validate_trade(SNAPSHOT)

    def test_http_errors_never_include_the_key(self):
        client, _ = make_client(FakeResponse({}, status=401))
        with pytest.raises(ProviderUnavailable) as excinfo:
            client.validate_trade(SNAPSHOT)
        assert FAKE_KEY not in str(excinfo.value)

    def test_transport_error_never_includes_the_key(self):
        client, _ = make_client(OSError("connection reset by peer"))
        with pytest.raises(ProviderUnavailable) as excinfo:
            client.validate_trade(SNAPSHOT)
        assert FAKE_KEY not in str(excinfo.value)


# ─────────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────────

class TestSuccessfulValidation:

    def test_returns_a_verdict(self):
        client, _ = make_client(ok_response())
        result = client.validate_trade(SNAPSHOT)
        assert isinstance(result, Validation)
        assert result.verdict is Verdict.APPROVE
        assert result.confidence_adjust == 0

    def test_posts_to_the_xai_endpoint(self):
        client, calls = make_client(ok_response())
        client.validate_trade(SNAPSHOT)
        assert calls["url"].startswith("https://api.x.ai/v1")
        assert "chat/completions" in calls["url"]

    def test_sends_the_key_in_the_header(self):
        client, calls = make_client(ok_response())
        client.validate_trade(SNAPSHOT)
        assert calls["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"

    def test_snapshot_is_sent_as_numbers(self):
        client, calls = make_client(ok_response())
        client.validate_trade(SNAPSHOT)
        user = calls["json"]["messages"][-1]["content"]
        sent = json.loads(user)
        assert sent["price"] == 1.10000
        assert sent["indicators"]["rsi"] == 55.0

    def test_records_latency(self):
        client, _ = make_client(ok_response())
        assert client.validate_trade(SNAPSHOT).latency_ms >= 0

    def test_temperature_is_zero(self):
        """A validator should be reproducible."""
        client, calls = make_client(ok_response())
        client.validate_trade(SNAPSHOT)
        assert calls["json"]["temperature"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Failure modes: each must be distinguishable
# ─────────────────────────────────────────────────────────────────────────────

class TestFailureModesAreDistinct:

    def test_timeout_raises_timeout(self):
        import requests
        client, _ = make_client(requests.exceptions.Timeout("slow"))
        with pytest.raises(ProviderTimeout):
            client.validate_trade(SNAPSHOT)

    def test_timeout_is_never_retried(self):
        """One attempt only. A late retry costs money for nothing."""
        attempts = []
        ring = Keyring(env={"XAI_API_KEY": FAKE_KEY})

        def post(url, *, headers, json, timeout):
            attempts.append(1)
            raise TimeoutError("slow")

        client = XAIClient(ring, http_post=post)
        with pytest.raises(ProviderError):
            client.validate_trade(SNAPSHOT)
        assert len(attempts) == 1, f"expected 1 attempt, made {len(attempts)}"

    def test_transport_error_raises_unavailable(self):
        client, _ = make_client(ConnectionResetError("reset"))
        with pytest.raises(ProviderUnavailable):
            client.validate_trade(SNAPSHOT)

    def test_http_500_raises_unavailable(self):
        client, _ = make_client(FakeResponse({}, status=500))
        with pytest.raises(ProviderUnavailable) as excinfo:
            client.validate_trade(SNAPSHOT)
        assert "500" in str(excinfo.value)

    def test_malformed_json_raises_invalid(self):
        bad = FakeResponse({"choices": [{"message": {"content": "not json"}}]})
        client, _ = make_client(bad)
        with pytest.raises(ProviderResponseInvalid):
            client.validate_trade(SNAPSHOT)

    def test_wrong_response_shape_raises_invalid(self):
        client, _ = make_client(FakeResponse({"unexpected": "shape"}))
        with pytest.raises(ProviderResponseInvalid):
            client.validate_trade(SNAPSHOT)

    def test_json_array_raises_invalid(self):
        bad = FakeResponse({"choices": [{"message": {"content": "[1,2,3]"}}]})
        client, _ = make_client(bad)
        with pytest.raises(ProviderResponseInvalid):
            client.validate_trade(SNAPSHOT)

    def test_invalid_verdict_raises_invalid(self):
        bad = FakeResponse({"choices": [{"message": {"content": json.dumps(
            {"verdict": "MAYBE", "confidence_adjust": 0})}}]})
        client, _ = make_client(bad)
        with pytest.raises(ProviderResponseInvalid) as excinfo:
            client.validate_trade(SNAPSHOT)
        assert "verdict" in str(excinfo.value).lower()

    def test_markdown_fences_are_tolerated(self):
        """Models wrap JSON in fences; that is not a malformed response."""
        fenced = json.dumps({
            "verdict": "APPROVE", "confidence_adjust": 0,
            "rationale": "ok", "cited_values": {}})
        body = {"choices": [{"message": {"content":
            f"```json\n{fenced}\n```"}}]}
        client, _ = make_client(FakeResponse(body))
        assert client.validate_trade(SNAPSHOT).verdict is Verdict.APPROVE

    def test_think_blocks_are_stripped(self):
        fenced = json.dumps({
            "verdict": "REDUCE", "confidence_adjust": -5,
            "rationale": "ok", "cited_values": {}})
        body = {"choices": [{"message": {"content":
            f"<think>internal reasoning</think>{fenced}"}}]}
        client, _ = make_client(FakeResponse(body))
        assert client.validate_trade(SNAPSHOT).verdict is Verdict.REDUCE

    def test_every_failure_is_a_provider_error(self):
        """One catchable family, so the engine can fail safe uniformly."""
        import requests
        for response in (
            requests.exceptions.Timeout("slow"),
            ConnectionResetError("reset"),
            FakeResponse({}, status=503),
            FakeResponse({"bad": "shape"}),
        ):
            client, _ = make_client(response)
            with pytest.raises(ProviderError):
                client.validate_trade(SNAPSHOT)


# ─────────────────────────────────────────────────────────────────────────────
# The snapshot is the contract with the hallucination checker
# ─────────────────────────────────────────────────────────────────────────────

class TestSnapshotContract:

    def test_all_values_flattens_every_number(self):
        values = SNAPSHOT.all_values()
        assert values["price"] == 1.10000
        assert values["ema20"] == 1.09900
        assert values["rsi"] == 55.0
        assert values["atr"] == 0.00040

    def test_payload_rounds_for_transmission(self):
        payload = SNAPSHOT.to_payload()
        assert payload["price"] == 1.10000
        assert set(payload) == {"symbol", "timeframe", "price",
                                "indicators", "context"}

    def test_snapshot_carries_no_free_text(self):
        """A narrative field would be unverifiable, so none exists.

        Checked against whole field names rather than substrings: 'context'
        contains the letters 'text', but it holds numbers, not prose.
        """
        import dataclasses

        fields = {f.name for f in dataclasses.fields(Snapshot)}
        assert fields == {"symbol", "timeframe", "price", "indicators",
                          "context"}

        prose_names = {"text", "narrative", "note", "comment", "notes",
                       "description", "summary", "reasoning", "message"}
        assert not (fields & prose_names), (
            f"unexpected prose field(s): {fields & prose_names}"
        )

        # And the values carried are numeric, so prose cannot smuggle in.
        snapshot = SNAPSHOT
        for mapping in (snapshot.indicators, snapshot.context):
            for key, value in mapping.items():
                assert isinstance(key, str)
                assert isinstance(value, (int, float)), (
                    f"{key} is {type(value).__name__}; snapshot values must "
                    f"be numeric so the hallucination check can bound them"
                )

    def test_a_cited_value_outside_the_snapshot_is_detectable(self):
        """The premise the hallucination check rests on."""
        values = SNAPSHOT.all_values()
        assert "momentum" not in values
        # A model citing 'momentum' cites something not in the snapshot.

    def test_cited_values_are_parsed(self):
        client, _ = make_client(ok_response(
            cited_values={"ema20": 1.0990, "rsi": 55.0}))
        result = client.validate_trade(SNAPSHOT)
        assert result.cited_values == {"ema20": 1.0990, "rsi": 55.0}

    def test_reduce_verdict_carries_a_negative_adjust(self):
        client, _ = make_client(ok_response(
            verdict="REDUCE", confidence_adjust=-8))
        result = client.validate_trade(SNAPSHOT)
        assert result.verdict is Verdict.REDUCE
        assert result.confidence_adjust == -8


class TestVerdictAuthority:
    """The model validates; it never authorises."""

    def test_verdict_set_has_no_authorising_member(self):
        """No value means 'execute'. That decision belongs to the gate."""
        names = {v.value for v in Verdict}
        assert names == {"APPROVE", "REDUCE", "REJECT"}
        assert not (names & {"EXECUTE", "BUY", "SELL", "ALLOW"})

    def test_reject_is_a_fail_safe_marker(self):
        assert Validation(verdict=Verdict.REJECT, confidence_adjust=-20,
                          rationale="").is_fail_safe is True
        assert Validation(verdict=Verdict.APPROVE, confidence_adjust=0,
                          rationale="").is_fail_safe is False

    def test_client_cannot_place_an_order(self):
        """Structural: the module has no gateway or executor surface."""
        from engine import llm_client
        source = (llm_client.__file__ and
                  open(llm_client.__file__, encoding="utf-8").read())
        for forbidden in ("order_send", "order_check", "MetaTrader5",
                          "positions_get", "build_executor"):
            assert forbidden not in source, (
                f"the LLM client must not reference {forbidden}"
            )