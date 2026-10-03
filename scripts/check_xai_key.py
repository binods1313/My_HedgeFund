"""Check the XAI_API_KEY: is it picked up, and does it authenticate?

Never prints the key. Reports a sha256 fingerprint prefix, length, and the
outcome of one minimal API call.

Usage:
    python scripts/check_xai_key.py
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from engine.keys import KeyNotConfigured, keyring

# xAI serves an OpenAI-compatible API.
XAI_BASE = "https://api.x.ai/v1"
XAI_MODEL = "grok-3-mini"          # cheapest capable model
MAX_TOKENS = 8                     # a credential check, not a real prompt


def fingerprint(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()[:12]}"


def main() -> int:
    ring = keyring()

    print("=" * 70)
    print("  XAI_API_KEY CHECK")
    print("=" * 70)
    print(f"  describe(): {ring.describe('xai')}")
    print()

    try:
        key = ring.require("xai")
    except KeyNotConfigured as exc:
        print(f"  [FAIL] {exc}")
        return 2

    # The value itself is never printed.
    print(f"  fingerprint : {fingerprint(key)}")
    print(f"  length       : {len(key)} chars")
    print(f"  prefix shape : {'ok' if key.startswith('xai-') else 'UNEXPECTED (not xai-)'}")

    if len(key) < 20:
        print("  [FAIL] key is too short to be a real credential")
        return 2
    print()

    # -- live authentication ------------------------------------------------
    print(f"  contacting {XAI_BASE}/chat/completions (model={XAI_MODEL}) ...")
    try:
        import requests
    except ImportError:
        print("  [SKIP] requests is not installed")
        return 2

    try:
        response = requests.post(
            f"{XAI_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": XAI_MODEL,
                "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                "max_tokens": MAX_TOKENS,
                "temperature": 0,
            },
            timeout=45,
        )
    except Exception as exc:   # noqa: BLE001 - reported, not raised
        print(f"  [FAIL] network error contacting xAI: {type(exc).__name__}: {exc}")
        return 3

    print(f"  HTTP status : {response.status_code}")

    if response.status_code == 200:
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            usage = body.get("usage", {})
            print(f"  model       : {body.get('model', '?')}")
            print(f"  reply       : {str(content).strip()!r}")
            print(f"  tokens      : {usage.get('total_tokens', '?')}")
        except Exception as exc:   # noqa: BLE001
            print(f"  [WARN] authenticated but the response shape was unexpected: {exc}")
        print()
        print("  RESULT: KEY IS VALID — xAI accepted it and returned a completion.")
        return 0

    # Failure: report status and whatever the API says, without echoing the key.
    print()
    try:
        detail = response.json()
        message = detail.get("error", {}).get("message", "")
        err_type = detail.get("error", {}).get("type", "")
        if message:
            print(f"  API message : {message[:200]}")
        if err_type:
            print(f"  API type    : {err_type}")
    except Exception:   # noqa: BLE001
        print(f"  body        : {response.text[:200]}")

    print()
    if response.status_code in (401, 403):
        print("  RESULT: KEY REJECTED — authentication or authorisation failed.")
        print("          Check the key was pasted without quotes or trailing space,")
        print("          and that the xAI account has credit.")
        return 1
    if response.status_code == 404:
        print("  RESULT: model not found. The KEY may be valid; try a different model.")
        return 1
    if response.status_code == 429:
        print("  RESULT: rate limited or out of credit. The key parsed.")
        return 1
    print(f"  RESULT: unexpected HTTP {response.status_code}")
    return 1


if __name__ == "__main__":
    sys.exit(main())