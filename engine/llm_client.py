"""xAI provider client for Phase 4.

A thin, typed wrapper over the OpenAI-compatible xAI endpoint. The LLM is a
validation layer, not a strategy: nothing here can authorise an order, and the
executable rule remains apply_llm_adjustment().

Design constraints, all of which come from Phase 0 experience:

  - The key is read through engine.keys.ProviderKeyring, never from os.environ
    here, and never appears in a repr, str, log line or exception message.
  - Every failure mode resolves to an explicit error type carrying a safe
    message. A network error must never be reported as a successful HOLD,
    because those mean different things to an operator: one is the model
    declining, the other is the system not working.
  - Timeouts fail safe. There is no retry: a retried validation request can
    arrive after the decision window and a duplicate call costs money for
    nothing.
  - The snapshot the model receives is built here and is exact. The caller
    cannot inject free text, which is how hallucination checking works at all.

Nothing in this module places an order, reads the terminal, or writes state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from engine.keys import KeyNotConfigured, Keyring, keyring

XAI_BASE_URL = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-4"
DEFAULT_TIMEOUT = 25.0
MAX_OUTPUT_TOKENS = 400

PROVIDER = "xai"


class ProviderError(RuntimeError):
    """Base for client failures. Messages never contain the key."""


class ProviderNotConfigured(ProviderError):
    """No usable key for the provider."""


class ProviderTimeout(ProviderError):
    """The request did not complete in time. Fails safe; never retried."""


class ProviderUnavailable(ProviderError):
    """Transport failure or a non-2xx response."""


class ProviderResponseInvalid(ProviderError):
    """The response was not the shape the OpenAI-compatible API promises."""


class Verdict(str, Enum):
    """The model's opinion.

    VALIDATE only. The model has no authority to authorise or reject a trade;
    it can only reduce confidence or flag a problem, and
    apply_llm_adjustment() converts that into the executable decision.
    """

    APPROVE = "APPROVE"
    REDUCE = "REDUCE"
    REJECT = "REJECT"


@dataclass(frozen=True)
class Snapshot:
    """The exact computed values the model is allowed to reason about.

    Constructed from numbers only. There is deliberately no free-text field: a
    narrative the caller supplies is exactly the thing the hallucination check
    cannot verify, so it is not accepted here.
    """

    symbol: str
    timeframe: str
    price: float
    indicators: dict[str, float] = field(default_factory=dict)
    context: dict[str, float] = field(default_factory=dict)

    def to_payload(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "price": round(float(self.price), 5),
            "indicators": {k: round(float(v), 5)
                           for k, v in sorted(self.indicators.items())},
            "context": {k: round(float(v), 5)
                        for k, v in sorted(self.context.items())},
        }

    def all_values(self) -> dict[str, float]:
        """Every number the model is shown, keyed by name.

        The hallucination checker compares against this. A value the model cites
        that is not in here did not come from the snapshot.
        """
        out = {"price": float(self.price)}
        out.update({k: float(v) for k, v in self.indicators.items()})
        out.update({k: float(v) for k, v in self.context.items()})
        return out


@dataclass(frozen=True)
class Validation:
    """One model response, reduced to what the engine needs."""

    verdict: Verdict
    confidence_adjust: float     # in [-20, 0]
    rationale: str
    cited_values: dict[str, float] = field(default_factory=dict)
    model: str = ""
    latency_ms: int = 0

    @property
    def is_fail_safe(self) -> bool:
        return self.verdict is Verdict.REJECT


class XAIClient:
    """Client for the xAI chat-completions API.

    Stateless. One instance may be reused; it holds no per-call state, so a
    request cannot observe a previous request's data.
    """

    def __init__(self, keyring_: Keyring | None = None, *,
                 model: str = DEFAULT_MODEL,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_tokens: int = MAX_OUTPUT_TOKENS,
                 http_post=None):
        self._keys = keyring_ or keyring()
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        # Injectable for tests; production uses requests.post directly.
        self._post = http_post or _default_post()

    # -- key handling ----------------------------------------------------

    def describe(self) -> str:
        """Redacted status, safe to log."""
        return self._keys.describe(PROVIDER)

    def is_configured(self) -> bool:
        return self._keys.is_configured(PROVIDER)

    def _api_key(self) -> str:
        try:
            return self._keys.require(PROVIDER)
        except KeyNotConfigured as exc:
            # The message names the env var, never a value.
            raise ProviderNotConfigured(str(exc)) from None

    # -- the call --------------------------------------------------------

    def validate_trade(self, snapshot: Snapshot) -> Validation:
        """Ask the model to assess a snapshot.

        Raises a Provider* subclass on every failure. It never returns a
        Validation on the error path, because a caller that treats an
        exception as 'the model declined' would be conflating a broken
        provider with a considered opinion.
        """
        import time as _time

        key = self._api_key()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(
                    snapshot.to_payload(), indent=2)},
            ],
            "temperature": 0,
            "max_tokens": self.max_tokens,
        }

        started = _time.monotonic()
        try:
            response = self._post(
                f"{XAI_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
        except TimeoutError as exc:
            # Fails safe and is NOT retried: a late retry can land after the
            # decision window and cost money for nothing.
            raise ProviderTimeout(
                f"{PROVIDER} did not respond within {self.timeout:g}s"
            ) from exc
        except Exception as exc:   # noqa: BLE001
            if _is_transport_timeout(exc):
                raise ProviderTimeout(
                    f"{PROVIDER} did not respond within {self.timeout:g}s"
                ) from exc
            raise ProviderUnavailable(
                f"{PROVIDER} request failed: {type(exc).__name__}"
            ) from exc
        latency_ms = int((_time.monotonic() - started) * 1000)

        if response.status_code != 200:
            raise ProviderUnavailable(
                f"{PROVIDER} returned HTTP {response.status_code}")

        return self._parse(response, latency_ms)

    def _parse(self, response, latency_ms: int) -> Validation:
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderResponseInvalid(
                f"{PROVIDER} response did not match the expected shape: "
                f"{type(exc).__name__}"
            ) from None

        if isinstance(content, list):
            # Some providers return content blocks rather than a string.
            try:
                content = "".join(
                    block.get("text", "") for block in content
                    if isinstance(block, dict))
            except AttributeError:
                raise ProviderResponseInvalid(
                    f"{PROVIDER} returned an unreadable content block"
                ) from None

        cleaned = _strip_thinking(str(content))
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ProviderResponseInvalid(
                f"{PROVIDER} returned malformed JSON: {exc.msg}"
            ) from None
        if not isinstance(parsed, dict):
            raise ProviderResponseInvalid(
                f"{PROVIDER} returned a JSON {type(parsed).__name__}, not an object")

        verdict_raw = str(parsed.get("verdict", "")).upper().strip()
        try:
            verdict = Verdict(verdict_raw)
        except ValueError:
            raise ProviderResponseInvalid(
                f"{PROVIDER} returned an invalid verdict "
                f"{verdict_raw[:24]!r}; expected APPROVE, REDUCE or REJECT"
            ) from None

        adjust = _finite(parsed.get("confidence_adjust"), default=None)
        rationale = str(parsed.get("rationale", ""))[:2000]

        cited = {}
        raw_cited = parsed.get("cited_values")
        if isinstance(raw_cited, dict):
            cited = {k: _finite(v, default=0.0)
                     for k, v in raw_cited.items()}

        return Validation(
            verdict=verdict,
            confidence_adjust=adjust,
            rationale=rationale,
            cited_values=cited,
            model=self.model,
            latency_ms=latency_ms,
        )

    def __repr__(self) -> str:
        return f"XAIClient(model={self.model!r}, key={self.describe()})"

    def __str__(self) -> str:
        return self.__repr__()


_SYSTEM_PROMPT = (
    "You are a quantitative risk reviewer. You will receive a JSON object of "
    "exactly computed indicator values.\n"
    "Reference ONLY numbers present in that object. Never invent, estimate or "
    "recall a value that is not in it.\n\n"
    "Reply with ONLY a JSON object:\n"
    "{\n"
    '  "verdict": "APPROVE" | "REDUCE" | "REJECT",\n'
    '  "confidence_adjust": <integer from -20 to 0>,\n'
    '  "rationale": "<two sentences citing only the supplied numbers>",\n'
    '  "cited_values": {"<field>": <number you used>}\n'
    "}\n"
    "confidence_adjust must be zero or negative; you may only reduce."
)


def _strip_thinking(text: str) -> str:
    """Remove <think> blocks and markdown fences some models emit."""
    import re as _re

    text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL)
    text = _re.sub(r"```(?:json)?\s*", "", text)
    return text.strip()


def _finite(value, default):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    f = float(value)
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def _is_transport_timeout(exc: BaseException) -> bool:
    """True when an exception represents a timeout rather than a hard failure.

    requests raises its own Timeout classes, and a caller may inject a plain
    TimeoutError, so both are recognised. Class-name matching avoids importing
    requests at module scope, which would make the module unimportable without
    the dependency installed.
    """
    if isinstance(exc, TimeoutError):
        return True
    return "timeout" in type(exc).__name__.lower()


def _default_post():
    """The real HTTP call, imported lazily so tests need no network."""
    import requests

    def post(url, *, headers, json, timeout):
        return requests.post(url, headers=headers, json=json, timeout=timeout)

    return post