"""Tests for the Time Machine pre-approval hook (fail-open, advisory)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.journal import Journal
from engine.notifications import PendingTrade
from engine.notifications.telegram_service import _build_approval_message
from engine.research.timemachine_hook import (
    default_target,
    run_timemachine,
    _summary_from_output,
)

ROOT = Path(__file__).resolve().parent.parent.parent
EURUSD_CACHE = ROOT / "data" / "market_bars" / "EURUSD__M15.json"


def tm_cfg(**over):
    base = {"enabled": True, "replays": 200, "seed": 19, "window": 2,
            "until": "23:45", "timeout_seconds": 60, "show_in_telegram": True}
    base.update(over)
    return SimpleNamespace(**base)


def test_default_target_buy():
    assert default_target("BUY", 1.10, 1.09) == pytest.approx(1.12)
    assert default_target("SELL", 1.10, 1.11) == pytest.approx(1.08)
    assert default_target("BUY", 0, 1.09) is None


def test_summary_from_output_parses():
    data = {"mc": {"p_target": 35.2, "p_stop": 64.8, "p_neither": 0.1,
                   "p_green": 35.2, "avg_R": -0.062, "replays": 2000,
                   "seed": 19, "until": "23:45", "pool_sessions": 39,
                   "pool_from": "2026-09-08", "pool_to": "2026-11-02"},
            "plan": {"target_r": 2.0},
            "verdict": "Close to break-even",
            "verdict_notes": [], "warnings": [], "source": "mt5"}
    s = _summary_from_output(data)
    assert s["p_target"] == 35.2
    assert s["verdict"] == "Close to break-even"
    assert s["pool_sessions"] == 39


def test_telegram_message_includes_timemachine():
    trade = PendingTrade(
        trade_id="abc1234", symbol="EURUSD", direction="BUY",
        entry=1.1175, stop_loss=1.1169, take_profit=1.1187,
        lot_size=0.1, confidence=80.0, rationale="confluence",
        timemachine={"verdict": "Leans toward the target",
                     "p_target": 40.8, "p_stop": 59.2, "avg_R": 0.1},
    )
    msg = _build_approval_message(trade)
    assert "Time Machine" in msg
    assert "Leans toward the target" in msg
    assert "Target first" in msg


def test_telegram_message_without_timemachine():
    trade = PendingTrade(
        trade_id="abc1234", symbol="EURUSD", direction="BUY",
        entry=1.1175, stop_loss=1.1169, take_profit=1.1187,
        lot_size=0.1, confidence=80.0, rationale="confluence",
    )
    msg = _build_approval_message(trade)
    assert "Time Machine" not in msg


def _kinds(journal, category="timemachine"):
    kinds = []
    for e in journal.recent_events(limit=20, category=category):
        try:
            kinds.append(json.loads(e["context_json"]).get("kind"))
        except Exception:  # noqa: BLE001
            kinds.append(None)
    return kinds


@pytest.mark.skipif(not EURUSD_CACHE.exists(), reason="requires EURUSD M15 cache")
def test_run_timemachine_integration(tmp_path):
    journal = Journal(tmp_path / "tm.db")
    summary = asyncio.run(run_timemachine(
        tm_cfg(replays=200),
        symbol="EURUSD", direction="BUY", entry=1.1175, stop=1.1169,
        target=1.1187, journal=journal,
    ))
    assert summary is not None
    assert summary["verdict"] is not None
    assert summary["p_target"] is not None
    assert "timemachine_ok" in _kinds(journal)


def test_run_timemachine_fail_open(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "tm.db")

    async def boom(*a, **k):
        raise RuntimeError("subprocess spawn failed")

    monkeypatch.setattr(
        "engine.research.timemachine_hook.asyncio.create_subprocess_exec", boom)
    summary = asyncio.run(run_timemachine(
        tm_cfg(), symbol="EURUSD", direction="BUY",
        entry=1.1175, stop=1.1169, target=1.1187, journal=journal,
    ))
    assert summary is None
    assert "timemachine_error" in _kinds(journal)


def test_run_timemachine_no_target_journals_error(tmp_path):
    journal = Journal(tmp_path / "tm.db")
    summary = asyncio.run(run_timemachine(
        tm_cfg(), symbol="EURUSD", direction="BUY",
        entry=0, stop=0, target=None, journal=journal,
    ))
    assert summary is None
    assert "timemachine_error" in _kinds(journal)
