"""LLM validation layer.

The LLM is NOT the strategy. Quantitative strategies produce a `signal_score`;
this module can only validate that signal. It may REDUCE confidence or REJECT.
It can never raise a score, and it can never create a trade.

Two distinct surfaces live here:

  1. `apply_llm_adjustment()` — the pure, executable rule. Given a computed
     signal score, an LLM verdict and an adjustment in [-20, 0], it returns the
     final score and whether a trade is permitted. No I/O, no network, fully
     unit-testable. This is the ONLY place where LLM confidence affects whether
     a trade happens.

  2. `get_ai_decision()` — the legacy display path used by the Streamlit app.
     It asks the model for an opinion and returns it for rendering. Its
     `confidence` field is LLM-asserted and is NOT an execution gate; the app
     labels it as such. Phase 1/7 replaces this with the computed confidence
     engine feeding `apply_llm_adjustment()`.

Fail-safe policy: every error path (timeout, transport error, non-200, malformed
JSON, schema violation, hallucinated numbers, out-of-range adjustment) results
in HOLD / no trade. Never the opposite.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

import requests

import config
from config_loader import LlmTolerances, load_config

# The LLM may only move confidence within this range, and only downwards.
ADJUST_MIN = -20.0
ADJUST_MAX = 0.0

VALID_VERDICTS = ("APPROVE", "REDUCE", "REJECT")

Decision = Literal["TRADE", "NO_TRADE"]

_HOLD_RESPONSE = {
    "decision": "HOLD",
    "confidence": 60,
    "strength": "NEUTRAL",
    "reasoning": "Safety HOLD — see reason field.",
    "risk_reward": "N/A",
    "indicator_snapshot": {},
}


@dataclass(frozen=True)
class AdjustmentResult:
    """Outcome of applying an LLM verdict to a computed signal score."""

    final_score: float
    decision: Decision
    reason: str


def apply_llm_adjustment(
    signal_score: float,
    verdict: str,
    adjust: float,
    threshold: float,
    *,
    adjust_min: float = ADJUST_MIN,
    adjust_max: float = ADJUST_MAX,
) -> AdjustmentResult:
    """Apply an LLM verdict to a computed signal score.

    Rules, in order:
      1. `verdict` must be APPROVE / REDUCE / REJECT. Anything else -> NO_TRADE.
      2. A REJECT verdict is a hard stop -> NO_TRADE.
      3. `adjust` must be a real number in [adjust_min, adjust_max] = [-20, 0].
         A positive value (an attempt to raise confidence) or any out-of-range
         or non-numeric value -> NO_TRADE. We reject rather than clamp, because
         clamping would silently accept a malformed or adversarial response.
      4. `final_score = signal_score + adjust`, which by construction can never
         exceed `signal_score` because adjust <= 0.
      5. The threshold is applied AFTER the adjustment, strictly greater than:
         `final_score > threshold` is required to trade. A score exactly equal to
         the threshold does not trade.

    Returns an AdjustmentResult; never raises for bad input.
    """
    verdict_norm = str(verdict).strip().upper() if verdict is not None else ""

    if verdict_norm not in VALID_VERDICTS:
        return AdjustmentResult(
            final_score=float(signal_score),
            decision="NO_TRADE",
            reason=f"Invalid LLM verdict {verdict!r}; expected one of {VALID_VERDICTS}",
        )

    if verdict_norm == "REJECT":
        return AdjustmentResult(
            final_score=float(signal_score),
            decision="NO_TRADE",
            reason="LLM rejected the signal",
        )

    if isinstance(adjust, bool) or not isinstance(adjust, (int, float)):
        return AdjustmentResult(
            final_score=float(signal_score),
            decision="NO_TRADE",
            reason=f"Non-numeric confidence adjustment {adjust!r}",
        )
    if adjust != adjust:  # NaN
        return AdjustmentResult(
            final_score=float(signal_score),
            decision="NO_TRADE",
            reason="confidence_adjust is NaN",
        )
    if not (adjust_min <= adjust <= adjust_max):
        return AdjustmentResult(
            final_score=float(signal_score),
            decision="NO_TRADE",
            reason=(
                f"confidence_adjust {adjust} outside allowed range "
                f"[{adjust_min}, {adjust_max}]; the LLM may only reduce confidence"
            ),
        )

    final_score = float(signal_score) + float(adjust)
    # Defensive: adjust <= 0 and adjust_min < 0 guarantee this, but the whole
    # point of this layer is that the invariant is enforced rather than assumed.
    if final_score > signal_score:
        final_score = float(signal_score)

    if final_score > threshold:
        return AdjustmentResult(
            final_score=final_score,
            decision="TRADE",
            reason=(
                f"final_score {final_score:.1f} > threshold {threshold:.1f} "
                f"after LLM adjustment {adjust:+.1f}"
            ),
        )

    return AdjustmentResult(
        final_score=final_score,
        decision="NO_TRADE",
        reason=(
            f"final_score {final_score:.1f} not strictly greater than "
            f"threshold {threshold:.1f}"
        ),
    )


def _safe_hold(reason: str, snapshot: dict | None = None) -> dict:
    """Return a canonical HOLD with a logged reason. Never raises."""
    return {**_HOLD_RESPONSE, "reasoning": reason, "indicator_snapshot": snapshot or {}}


def _config_tolerances() -> LlmTolerances:
    """Read tolerances from config.yaml.

    A config error must not silently fall back to permissive defaults, so this
    lets the exception propagate to the caller, which converts it to HOLD.
    """
    return load_config().llm.hallucination_tolerances


_REQUIRED_M15 = ("close", "ema20", "ema50", "rsi", "macd_hist", "atr")
_REQUIRED_H4 = ("close", "ema50", "rsi")


def _numeric_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return None if f != f else f  # drop NaN


def build_indicator_snapshot(df_m15, df_h4) -> dict:
    """Build the structured JSON snapshot that is sent to the LLM.

    Contains exact computed values only. Returns {} on unusable input so the
    caller fails safe to HOLD rather than sending the model a partial picture.
    """
    if df_m15 is None or df_h4 is None:
        return {}
    if getattr(df_m15, "empty", True) or getattr(df_h4, "empty", True):
        return {}
    if len(df_m15) < 1 or len(df_h4) < 1:
        return {}

    m = df_m15.iloc[-1]  # latest closed bar
    h = df_h4.iloc[-1]

    try:
        m15 = {
            "close": round(float(m["close"]), 5),
            "ema20": round(float(m["ema20"]), 5),
            "ema50": round(float(m["ema50"]), 5),
            "rsi": round(float(m["rsi"]), 2),
            "macd_hist": round(float(m["macd_hist"]), 5),
            "atr": round(float(m["atr"]), 5),
        }
        h4 = {
            "close": round(float(h["close"]), 5),
            "ema50": round(float(h["ema50"]), 5),
            "rsi": round(float(h["rsi"]), 2),
        }
    except (KeyError, TypeError, ValueError):
        # A missing indicator column means we cannot build a trustworthy
        # snapshot. Returning {} forces HOLD upstream.
        return {}

    snapshot = {"m15": m15, "h4": h4}

    # Reject NaN in any required field: the LLM must never see a hole.
    for section, keys in (("m15", _REQUIRED_M15), ("h4", _REQUIRED_H4)):
        for key in keys:
            if _numeric_or_none(snapshot[section].get(key)) is None:
                return {}

    return snapshot


# Snapshot field -> (tolerance attribute on LlmTolerances, is_absolute)
_TOLERANCE_MAP: dict[tuple[str, str], tuple[str, bool]] = {
    ("m15", "close"): ("price", False),
    ("m15", "ema20"): ("ema", False),
    ("m15", "ema50"): ("ema", False),
    ("m15", "atr"): ("atr", False),
    ("m15", "rsi"): ("rsi_points", True),
    ("m15", "macd_hist"): ("macd", False),
    ("h4", "close"): ("price", False),
    ("h4", "ema50"): ("ema", False),
    ("h4", "rsi"): ("rsi_points", True),
}


def _field_within_tolerance(cited: float, truth: float, tolerances: LlmTolerances,
                            attr: str, absolute: bool) -> bool:
    """Is `cited` close enough to `truth` for the given tolerance rule?"""
    tol = getattr(tolerances, attr)
    if absolute:
        return abs(cited - truth) <= tol

    if attr == "macd":
        # Relative, but with an absolute floor so a near-zero histogram does not
        # force an impossible match.
        limit = max(abs(truth) * tol, tolerances.macd_abs_floor)
        return abs(cited - truth) <= limit

    if truth == 0:
        # A relative test against zero is undefined; require an exact match.
        return cited == 0
    return abs(cited - truth) / abs(truth) <= tol


def _detect_hallucination(parsed: dict, snapshot: dict,
                          tolerances: LlmTolerances | None = None) -> str | None:
    """Check every cited number against the snapshot, field by field.

    Each number the model cites in its reasoning is tested against every
    snapshot field. If it is within tolerance of at least one field, it is
    accounted for. If it is within tolerance of none, it is an invented number
    and the response is rejected.

    Returns an error string when a hallucination is found, otherwise None.
    """
    if tolerances is None:
        tolerances = _config_tolerances()

    reasoning = str(parsed.get("reasoning", ""))
    cited_numbers = [float(n) for n in re.findall(r"\b\d+(?:\.\d+)?\b", reasoning)]
    if not cited_numbers:
        return None

    fields = {
        (section, field): _numeric_or_none(snapshot.get(section, {}).get(field))
        for section, field in _TOLERANCE_MAP
    }
    known = {k: v for k, v in fields.items() if v is not None}

    for cited in cited_numbers:
        matches = [
            key for key, truth in known.items()
            if _field_within_tolerance(cited, truth, tolerances, *_TOLERANCE_MAP[key])
        ]
        if matches:
            continue

        # Not near any known field. Report against the closest field so the
        # reason is actionable rather than "unknown number".
        key, truth = min(
            known.items(),
            key=lambda kv: abs(cited - kv[1]) / abs(kv[1]) if kv[1] else abs(cited),
        )
        section, field = key
        attr, absolute = _TOLERANCE_MAP[key]
        tol = getattr(tolerances, attr)
        deviation = abs(cited - truth)
        detail = (
            f"cited {cited:g} vs {section}.{field}={truth:g} "
            f"(difference {deviation:g}, tolerance "
            + (f"{tol:g} points" if absolute else f"{tol:g} relative")
            + ")"
        )
        return f"Hallucination detected: LLM cited a value matching no snapshot field: {detail}"

    return None


def get_ai_decision(df_m15=None, df_h4=None, symbol: str = "") -> dict:
    """Ask the LLM for an opinion for display purposes.

    NOTE: the returned `confidence` is LLM-asserted. It is NOT an execution
    gate and must not be used to decide whether to trade. The executable rule
    is `apply_llm_adjustment()`, which takes a computed signal score.

    Every failure path returns decision=HOLD.
    """
    api_key = getattr(config, "OPENROUTER_API_KEY", "")
    model = getattr(config, "OPENROUTER_MODEL", "openrouter/free")

    if not api_key or "YOUR_" in api_key:
        return _safe_hold("OPENROUTER_API_KEY not configured — HOLD.")

    if df_m15 is None or df_h4 is None:
        return _safe_hold("No market data provided to AI — HOLD.")

    snapshot = build_indicator_snapshot(df_m15, df_h4)
    if not snapshot:
        return _safe_hold("Empty indicator snapshot — HOLD.")

    system_prompt = (
        "You are a senior quantitative analyst validating a trade signal.\n"
        "You will receive a JSON object containing EXACT computed indicator values.\n"
        "You MUST base your reasoning ONLY on the numbers in the snapshot — do not "
        "invent, estimate, or hallucinate any values.\n\n"
        "Respond ONLY with valid JSON (no markdown, no backticks):\n"
        "{\n"
        '  "decision": "BUY" | "SELL" | "HOLD",\n'
        '  "confidence": <integer 0-100>,\n'
        '  "strength": "VERY STRONG" | "STRONG" | "MODERATE" | "NEUTRAL",\n'
        '  "reasoning": "<2 sentences citing ONLY exact values from the snapshot>",\n'
        '  "risk_reward": "1:2"\n'
        "}"
    )

    user_message = json.dumps(
        {
            "symbol": symbol,
            "indicator_snapshot": snapshot,
            "instruction": (
                "Analyse the snapshot. Reference only the values provided. "
                "Your reasoning must cite the exact numeric values from the snapshot."
            ),
        },
        indent=2,
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.1,
    }

    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://localhost",
                "X-Title": "AI Hedge Fund Terminal",
            },
            json=payload,
            timeout=25,
        )
    except Exception as exc:
        return _safe_hold(f"Connection timeout: {exc}", snapshot)

    if response.status_code != 200:
        return _safe_hold(f"API error {response.status_code} — defaulting to HOLD.", snapshot)

    try:
        raw = response.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, ValueError, AttributeError) as exc:
        return _safe_hold(f"Malformed API response structure: {exc}", snapshot)

    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    cleaned = re.sub(r"```json\s*|```", "", cleaned).strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return _safe_hold(f"LLM returned invalid JSON: {exc}", snapshot)

    if not isinstance(parsed, dict):
        return _safe_hold("LLM response was not a JSON object — HOLD.", snapshot)

    dec = str(parsed.get("decision", "")).upper().strip()
    if dec not in ("BUY", "SELL", "HOLD"):
        return _safe_hold(f"LLM returned invalid decision '{dec}' — HOLD.", snapshot)

    # No floor, no ceiling, no clamping: the value is reported verbatim as the
    # model's own assertion. It cannot gate execution. A non-numeric confidence
    # is a schema violation, and schema violations fail safe.
    conf = _numeric_or_none(parsed.get("confidence"))
    if conf is None:
        return _safe_hold(
            f"LLM returned non-numeric confidence "
            f"{parsed.get('confidence')!r} — HOLD.",
            snapshot,
        )
    if not (0 <= conf <= 100):
        return _safe_hold(
            f"LLM returned out-of-range confidence {conf} (expected 0-100) — HOLD.",
            snapshot,
        )

    strength = str(parsed.get("strength", "NEUTRAL")).upper()
    if strength not in ("VERY STRONG", "STRONG", "MODERATE", "NEUTRAL"):
        strength = "NEUTRAL"

    reasoning = str(parsed.get("reasoning", ""))

    try:
        hallucination_err = _detect_hallucination(parsed, snapshot)
    except Exception as exc:
        # Tolerances could not be loaded or compared; fail safe.
        return _safe_hold(f"Hallucination check unavailable: {exc}", snapshot)

    if hallucination_err:
        return {
            **_safe_hold(f"HOLD — {hallucination_err}", snapshot),
            "hallucination_detected": True,
        }

    return {
        "decision": dec,
        "confidence": conf,
        "strength": strength,
        "reasoning": reasoning,
        "risk_reward": str(parsed.get("risk_reward", "1:2")),
        "indicator_snapshot": snapshot,
        "hallucination_detected": False,
    }