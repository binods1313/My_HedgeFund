"""Time Machine observational pre-approval hook.

Runs the MT5 port of trader-timemachine (scripts/timemachine_mt5.py) as a
subprocess before a Telegram approval message is sent, and journal the
verdict/edge metrics. Strictly fail-open: any error, timeout, bad-plan
refusal, or malformed output is journaled as `timemachine_error` and the
pipeline continues unchanged — it never blocks, gates, or modifies an order.

Manual mode is unchanged: the same script stays runnable standalone.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_TARGET_MULT = 2.0  # 2R target when the signal carries no TP


def default_target(direction: str, entry: float, stop: float) -> Optional[float]:
    if entry is None or stop is None or entry <= 0 or stop <= 0:
        return None
    dist = abs(float(entry) - float(stop))
    if dist <= 0:
        return None
    return float(entry) + DEFAULT_TARGET_MULT * dist if str(direction).upper() == "BUY" \
        else float(entry) - DEFAULT_TARGET_MULT * dist


def _summary_from_output(data: dict) -> dict:
    mc = data.get("mc") or {}
    plan = data.get("plan") or {}
    return {
        "verdict": data.get("verdict"),
        "verdict_notes": data.get("verdict_notes", []),
        "warnings": data.get("warnings", []),
        "p_target": mc.get("p_target"),
        "p_stop": mc.get("p_stop"),
        "p_neither": mc.get("p_neither"),
        "p_green": mc.get("p_green"),
        "avg_R": mc.get("avg_R"),
        "target_r": plan.get("target_r"),
        "pool_sessions": mc.get("pool_sessions"),
        "pool_from": mc.get("pool_from"),
        "pool_to": mc.get("pool_to"),
        "replays": mc.get("replays"),
        "seed": mc.get("seed"),
        "until": mc.get("until"),
        "source": str(data.get("source") or "mt5"),
    }


async def run_timemachine(
    cfg: Any,
    *,
    symbol: str,
    direction: str,
    entry: float,
    stop: float,
    target: Optional[float] = None,
    stamp: Optional[str] = None,
    journal: Any = None,
) -> Optional[dict]:
    """Run the Time Machine once. Never raises; returns summary or None."""
    target = target if (target is not None and float(target) > 0) \
        else default_target(direction, entry, stop)
    if target is None:
        _journal_tm_error(journal, symbol, "no target derivable")
        return None

    date = stamp[:10] if stamp else \
        datetime.now(timezone.utc).date().isoformat()

    cmd = [
        sys.executable, "-m", "scripts.timemachine_mt5", "run",
        "--symbol", str(symbol),
        "--entry", str(entry),
        "--stop", str(stop),
        "--target", str(target),
        "--date", date,
        "--replays", str(int(getattr(cfg, "replays", 2000))),
        "--seed", str(int(getattr(cfg, "seed", 19))),
        "--window", str(int(getattr(cfg, "window", 2))),
        "--until", str(getattr(cfg, "until", "23:45")),
        "--json",
    ]
    timeout_s = float(getattr(cfg, "timeout_seconds", 60))

    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(ROOT),
            ),
            timeout=timeout_s,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"timemachine exit {proc.returncode}: {err.decode(errors='replace')[:200]}")
        data = json.loads(out.decode(errors="replace"))
        if not isinstance(data, dict):
            raise ValueError("timemachine returned non-dict output")
        summary = _summary_from_output(data)
        if journal is not None:
            try:
                journal.log_event(
                    "timemachine",
                    f"{symbol}: ok (verdict: {(summary.get('verdict') or '')[:80]})",
                    severity="INFO", source="engine",
                    context={"kind": "timemachine_ok", "symbol": symbol, **summary},
                )
            except Exception:  # noqa: BLE001
                pass
        return summary
    except Exception as exc:  # noqa: BLE001 - fail-open
        _journal_tm_error(journal, symbol, f"{type(exc).__name__}: {exc}")
        return None


def _journal_tm_error(journal, symbol: str, message: str) -> None:
    if journal is None:
        return
    try:
        journal.log_event(
            "timemachine",
            f"{symbol}: error: {message}",
            severity="WARNING", source="engine",
            context={"kind": "timemachine_error", "symbol": symbol,
                     "error": message},
        )
    except Exception:  # noqa: BLE001
        pass
