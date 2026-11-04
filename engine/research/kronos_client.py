"""Kronos forecast client (engine side).

Talks to the standalone Kronos inference server (see
D:/skills/Kronos/kronos_server.py). This module deliberately imports only
`requests` — the engine venv must never depend on torch/pandas-2.x, which
conflict with the engine's pinned deps.

Fail-open contract: every failure path returns None (or raises only when the
caller asked for strict=True). Kronos is an advisory research block — it must
never be able to block the pipeline just because the service is down.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import requests

DEFAULT_ENDPOINT = "http://127.0.0.1:8610"
DEFAULT_TIMEOUT_S = 60.0

PRICE_COLS = ("open", "high", "low", "close")


class KronosError(Exception):
    """Raised when strict=True and the service cannot be reached."""


class KronosClient:
    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.endpoint = endpoint.rstrip("/")
        self.timeout_s = timeout_s

    def health(self, timeout_s: Optional[float] = None) -> bool:
        try:
            r = requests.get(f"{self.endpoint}/health", timeout=timeout_s or self.timeout_s)
            return r.status_code == 200 and r.json().get("ok") is True
        except requests.RequestException:
            return False

    def forecast(
        self,
        symbol: str,
        bars: list[dict[str, Any]],
        *,
        pred_len: int = 8,
        sample_count: int = 8,
        top_k: int = 1,
        top_p: float = 1.0,
        timeout_s: Optional[float] = None,
        strict: bool = False,
    ) -> Optional[dict[str, Any]]:
        """Forecast the next `pred_len` bars from `bars` (OHLCV rows).

        `bars` rows may be either the engine cache shape
        ({"time","open","high","low","close","spread"}) or raw
        ({"time","open","high","low","close","volume"}).
        Returns None on any transport/service error unless strict=True.
        """
        clean = []
        for b in bars:
            row = {
                "time": b.get("time") or b.get("t"),
                "open": b.get("open"),
                "high": b.get("high"),
                "low": b.get("low"),
                "close": b.get("close"),
            }
            if "volume" in b:
                row["volume"] = b["volume"]
            elif "tick_volume" in b:
                row["volume"] = b["tick_volume"]
            clean.append(row)

        payload = {
            "symbol": symbol,
            "bars": clean,
            "pred_len": int(pred_len),
            "sample_count": int(sample_count),
            "top_k": int(top_k),
            "top_p": float(top_p),
        }
        try:
            r = requests.post(
                f"{self.endpoint}/forecast",
                json=payload,
                timeout=timeout_s or self.timeout_s,
            )
            if r.status_code != 200:
                raise KronosError(f"status {r.status_code}: {r.text[:200]}")
            return r.json()
        except (requests.RequestException, KronosError) as exc:
            if strict:
                raise KronosError(str(exc)) from exc
            return None

    @staticmethod
    def summarize(result: dict[str, Any]) -> dict[str, Any]:
        """Shrink a forecast response to the signal-relevant fields."""
        return {
            "symbol": result.get("symbol"),
            "direction": result.get("direction"),
            "agreement": result.get("agreement"),
            "expected_move_pct": result.get("expected_move_pct"),
            "horizon_close_mean": result.get("horizon_close_mean"),
            "last_close": result.get("last_close"),
            "pred_len": result.get("pred_len"),
            "sample_count": result.get("sample_count"),
            "fetched_at": time.time(),
        }
