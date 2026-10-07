"""One AgentRouter request. Prints no key, header, or URL.

Official base is https://agentrouter.org/v1 and the example model is gpt-5.
See github.com/agentrouter-org/docs. Override with AGENTROUTER_BASE_URL and
AGENTROUTER_MODEL. One request, max_tokens 1, no retry.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

TIMEOUT_S = 10
_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
_DEFAULT_BASE = "https://agentrouter.org/v1"
_DEFAULT_MODEL = "gpt-5"


def _resolves(host: str) -> bool:
    if not host:
        return False
    try:
        socket.getaddrinfo(host, 443)
    except OSError:
        return False
    return True


def main() -> None:
    load_dotenv(_ENV_PATH)
    base = os.environ.get("AGENTROUTER_BASE_URL", _DEFAULT_BASE).strip() or _DEFAULT_BASE
    parsed = urlparse(base if "://" in base else "https://" + base)
    host = parsed.hostname or ""
    key = os.environ.get("AGENTROUTER_API_KEY", "")
    model = os.environ.get("AGENTROUTER_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    present = bool(key.strip())
    resolves = _resolves(host)
    print(f"key present: {'yes' if present else 'no'}")
    print(f"host resolves: {'yes' if resolves else 'no'}")

    if not present or not resolves:
        print("HTTP status: N/A")
        print("FAIL")
        return

    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "temperature": 0,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )
    status = "N/A"
    ok = False
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            status = response.status
            body = response.read().decode("utf-8", errors="replace")
            ok = 200 <= status < 300 and "choices" in body
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception:
        status = "N/A"
    print(f"HTTP status: {status}")
    print("PASS" if ok else "FAIL")


if __name__ == "__main__":
    main()
