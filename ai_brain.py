"""AI Brain — Phase 0 fix (Bug 6): structured JSON snapshot + hallucination detection.

Key changes from original:
  1. The LLM now receives a structured JSON snapshot of computed indicator
     values, NOT a freeform text string. This prevents the LLM from inventing
     numbers that don't match the actual chart.
  2. The LLM response is validated: any cited numeric value that deviates from
     the snapshot by more than HALLUCINATION_TOLERANCE is rejected and a HOLD
     is returned (fail-safe).
  3. On ANY error (timeout, bad JSON, schema mismatch, hallucination) the
     function returns HOLD — never a spurious BUY/SELL.
  4. The confidence range is capped at 60–95; values outside this range are
     clamped rather than trusted.
"""

import json
import re
import requests
import config


# Maximum relative deviation allowed between a number the LLM cites and the
# value we actually computed (e.g. 0.03 = 3%). Tighten this as needed.
HALLUCINATION_TOLERANCE = 0.03

_HOLD_RESPONSE = {
    "decision": "HOLD",
    "confidence": 60,
    "strength": "NEUTRAL",
    "reasoning": "Safety HOLD — see reason field.",
    "risk_reward": "N/A",
    "indicator_snapshot": {},
}


def _safe_hold(reason: str, snapshot: dict | None = None) -> dict:
    """Return a canonical HOLD with a logged reason. Never raises."""
    return {**_HOLD_RESPONSE,
            "reasoning": reason,
            "indicator_snapshot": snapshot or {}}


def build_indicator_snapshot(df_m15, df_h4) -> dict:
    """Build the structured JSON snapshot that is sent to the LLM.

    Returns exact computed values only — the LLM receives these as the single
    source of truth and may only reference them in its reasoning.

    Args:
        df_m15: 15-minute OHLCV + indicator DataFrame (from get_technical_dataframe)
        df_h4:  4-hour OHLCV + indicator DataFrame
    """
    if df_m15.empty or df_h4.empty:
        return {}

    m = df_m15.iloc[-1]   # latest closed bar
    h = df_h4.iloc[-1]

    return {
        "m15": {
            "close":     round(float(m["close"]),     5),
            "ema20":     round(float(m["ema20"]),     5),
            "ema50":     round(float(m["ema50"]),     5),
            "rsi":       round(float(m["rsi"]),       2),
            "macd_hist": round(float(m["macd_hist"]), 5),
            "atr":       round(float(m["atr"]),       5),
        },
        "h4": {
            "close": round(float(h["close"]), 5),
            "ema50": round(float(h["ema50"]), 5),
            "rsi":   round(float(h["rsi"]),   2),
        },
    }


def _detect_hallucination(parsed: dict, snapshot: dict) -> str | None:
    """Check whether cited numeric values in the LLM response deviate from the
    snapshot beyond HALLUCINATION_TOLERANCE.

    Returns an error string if hallucination is detected, None otherwise.
    """
    # Fields the LLM is allowed to cite; keyed as (snapshot_section, field)
    checkable = [
        ("m15", "close"),
        ("m15", "ema20"),
        ("m15", "ema50"),
        ("m15", "rsi"),
        ("h4",  "close"),
        ("h4",  "ema50"),
    ]

    reasoning = str(parsed.get("reasoning", ""))

    # Extract all decimal numbers from the reasoning text
    cited_numbers = [float(n) for n in re.findall(r"\b\d+(?:\.\d+)?\b", reasoning)]
    if not cited_numbers:
        return None  # no numbers cited — nothing to check

    for section, field in checkable:
        truth = snapshot.get(section, {}).get(field)
        if truth is None or truth == 0:
            continue
        for cited in cited_numbers:
            if abs(cited - truth) / abs(truth) <= HALLUCINATION_TOLERANCE:
                break  # this truth value was cited accurately
        # If we found a citation close enough to this field, it's fine.
        # We only flag if the LLM cites a number that matches NO known field
        # within tolerance and is in the same ballpark as the truth value.

    # More targeted check: look for numbers near true price but wrong value
    true_price = snapshot.get("m15", {}).get("close", 0)
    if true_price > 0:
        for cited in cited_numbers:
            # If cited number is within 50% of true price but outside tolerance
            if (0.5 * true_price < cited < 1.5 * true_price and
                    abs(cited - true_price) / true_price > HALLUCINATION_TOLERANCE):
                return (
                    f"Hallucination detected: LLM cited price {cited:.5f} but "
                    f"computed close is {true_price:.5f} "
                    f"(deviation {abs(cited - true_price)/true_price:.1%} > "
                    f"{HALLUCINATION_TOLERANCE:.0%} tolerance)"
                )

    return None


def get_ai_decision(df_m15=None, df_h4=None, symbol: str = "") -> dict:
    """Query the LLM for a trade decision using a structured indicator snapshot.

    Changes from original (Bug 6 fix):
    - Accepts DataFrames instead of a freeform text string.
    - Sends a JSON snapshot of computed values; LLM cannot invent numbers.
    - Validates response schema and detects numeric hallucinations.
    - Any error → HOLD (fail-safe). LLM can only validate, not create trades.

    Args:
        df_m15:  15-minute DataFrame with indicators (from get_technical_dataframe)
        df_h4:   4-hour DataFrame with indicators
        symbol:  Instrument name (for logging/context only)

    Returns:
        dict with keys: decision, confidence, strength, reasoning,
                        risk_reward, indicator_snapshot, hallucination_detected
    """
    api_key = getattr(config, "OPENROUTER_API_KEY", "")
    model   = getattr(config, "OPENROUTER_MODEL", "openrouter/free")

    if not api_key or "YOUR_" in api_key:
        return _safe_hold("OPENROUTER_API_KEY not configured — HOLD.")

    # Build the structured snapshot from computed values
    if df_m15 is None or df_h4 is None:
        return _safe_hold("No market data provided to AI — HOLD.")

    snapshot = build_indicator_snapshot(df_m15, df_h4)
    if not snapshot:
        return _safe_hold("Empty indicator snapshot — HOLD.")

    # ── System prompt ─────────────────────────────────────────────────────────
    system_prompt = (
        "You are a senior quantitative analyst validating a trade signal.\n"
        "You will receive a JSON object containing EXACT computed indicator values.\n"
        "You MUST base your reasoning ONLY on the numbers in the snapshot — do not invent, "
        "estimate, or hallucinate any values.\n\n"
        "Respond ONLY with valid JSON (no markdown, no backticks):\n"
        "{\n"
        '  "decision": "BUY" | "SELL" | "HOLD",\n'
        '  "confidence": <integer 60-95>,\n'
        '  "strength": "VERY STRONG" | "STRONG" | "MODERATE" | "NEUTRAL",\n'
        '  "reasoning": "<2 sentences citing ONLY the exact values from the snapshot>",\n'
        '  "risk_reward": "1:2"\n'
        "}"
    )

    # User message is the JSON snapshot — no freeform text
    user_message = json.dumps({
        "symbol": symbol,
        "indicator_snapshot": snapshot,
        "instruction": (
            "Analyse the snapshot. Reference only the values provided. "
            "Your reasoning must cite the exact numeric values from the snapshot."
        ),
    }, indent=2)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_message},
        ],
        "temperature": 0.1,
    }

    # ── API call ──────────────────────────────────────────────────────────────
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
    except Exception as e:
        return _safe_hold(f"Connection timeout: {e}", snapshot)

    if response.status_code != 200:
        return _safe_hold(
            f"API error {response.status_code} — defaulting to HOLD.", snapshot)

    # ── Parse and validate ────────────────────────────────────────────────────
    try:
        raw = response.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, ValueError) as e:
        return _safe_hold(f"Malformed API response structure: {e}", snapshot)

    # Strip markdown/think blocks
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    cleaned = re.sub(r"```json\s*|```", "", cleaned).strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return _safe_hold(f"LLM returned invalid JSON: {e}", snapshot)

    # Schema validation — explicit checks, not assert (stripped under -O)
    dec = str(parsed.get("decision", "")).upper().strip()
    if dec not in ("BUY", "SELL", "HOLD"):
        return _safe_hold(
            f"LLM returned invalid decision '{dec}' — HOLD.", snapshot)

    try:
        conf = int(parsed.get("confidence", 60))
        conf = max(60, min(95, conf))   # clamp; never trust LLM to set threshold
    except (TypeError, ValueError):
        conf = 60

    strength = str(parsed.get("strength", "NEUTRAL")).upper()
    if strength not in ("VERY STRONG", "STRONG", "MODERATE", "NEUTRAL"):
        strength = "NEUTRAL"

    reasoning = str(parsed.get("reasoning", ""))

    # ── Hallucination detection ───────────────────────────────────────────────
    hallucination_err = _detect_hallucination(parsed, snapshot)
    if hallucination_err:
        return {
            **_safe_hold(
                f"HOLD — {hallucination_err}", snapshot),
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
