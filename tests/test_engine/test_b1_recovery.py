"""Mocked B1 recovery: a dropped terminal, then one recovery.

No live journal and no orders. The fake terminal is injected, and the
temporary journal is created by the test.
"""
from __future__ import annotations

import json
from pathlib import Path

from engine.__main__ import build_engine
from tests.test_engine.fake_mt5 import FakeMT5

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _kinds(journal, kind: str) -> list[dict]:
    rows = journal.query("SELECT severity, message, context_json FROM events")
    found = []
    for row in rows:
        context = json.loads(row["context_json"] or "{}")
        if context.get("kind") == kind:
            found.append({
                "severity": row["severity"],
                "message": row["message"],
                "context": context,
            })
    return found


def _equity_count(journal) -> int:
    return int(journal.query_one(
        "SELECT COUNT(*) AS n FROM equity_snapshots")["n"])


def _latest_heartbeat(journal) -> dict:
    return dict(journal.query_one(
        "SELECT id, connected, gateway_call_age_seconds FROM heartbeats "
        "ORDER BY id DESC LIMIT 1"))


def _engine(tmp_path, name: str):
    mt5 = FakeMT5()
    engine = build_engine(
        root=REPO_ROOT,
        journal_path=tmp_path / name,
        mt5_module=mt5,
    )
    return mt5, engine


def test_outage_then_recovery_resumes_telemetry_once(tmp_path):
    """None is offline. One offline row, one recovery row, equity resumes."""
    mt5, engine = _engine(tmp_path, "recovery.db")
    saved = mt5.account
    try:
        engine.run_once()
        healthy = _latest_heartbeat(engine.journal)
        assert healthy["connected"] == 1
        assert healthy["gateway_call_age_seconds"] is not None
        assert healthy["gateway_call_age_seconds"] >= 0
        equity_before = _equity_count(engine.journal)
        assert equity_before == 1

        mt5.account = None
        engine.run_once()
        engine.run_once()

        offline_beat = _latest_heartbeat(engine.journal)
        assert offline_beat["connected"] == 0, (
            "a None account_info() must clear the heartbeat connected flag"
        )
        assert _equity_count(engine.journal) == equity_before
        offline = _kinds(engine.journal, "terminal_offline")
        assert len(offline) == 1
        assert offline[0]["severity"] == "WARNING"
        assert _kinds(engine.journal, "terminal_recovered") == []
        assert "order_send" not in mt5.call_names()

        mt5.account = saved
        engine.gateway.status.next_retry_at = 0
        engine.run_once()

        recovered = _kinds(engine.journal, "terminal_recovered")
        assert len(recovered) == 1
        assert recovered[0]["severity"] == "INFO"
        assert recovered[0]["context"]["from_kind"] == "terminal_offline"
        assert recovered[0]["context"]["downtime_seconds"] is not None
        assert recovered[0]["context"]["downtime_seconds"] >= 0
        assert _latest_heartbeat(engine.journal)["connected"] == 1
        assert _equity_count(engine.journal) == equity_before + 1

        engine.run_once()
        assert len(_kinds(engine.journal, "terminal_offline")) == 1
        assert len(_kinds(engine.journal, "terminal_recovered")) == 1
        assert _equity_count(engine.journal) == equity_before + 2
        assert "order_send" not in mt5.call_names()
    finally:
        engine.gateway.stop()
        engine.journal.close()


def test_a_mismatch_does_not_auto_recover(tmp_path):
    """A readable wrong account stays blocked until the fingerprint matches."""
    mt5, engine = _engine(tmp_path, "mismatch.db")
    try:
        engine.run_once()
        equity_before = _equity_count(engine.journal)
        assert equity_before == 1

        mt5.account.login = 113363533
        engine.run_once()
        engine.run_once()

        beat = _latest_heartbeat(engine.journal)
        assert beat["connected"] == 1, (
            "a readable account is a successful read; the mismatch is a "
            "separate gate and must not be reported as a dead process"
        )
        assert _equity_count(engine.journal) == equity_before
        assert _kinds(engine.journal, "terminal_recovered") == []
        mismatches = _kinds(engine.journal, "account_mismatch")
        assert len(mismatches) == 1
        assert mismatches[0]["severity"] == "CRITICAL"

        mt5.account.login = 113363533
        engine.gateway.status.next_retry_at = 0
        engine.run_once()
        assert _kinds(engine.journal, "terminal_recovered") == []
        assert _equity_count(engine.journal) == equity_before

        mt5.account.login = 999999999
        engine.run_once()
        recovered = _kinds(engine.journal, "terminal_recovered")
        assert len(recovered) == 1
        assert recovered[0]["context"]["from_kind"] == "account_mismatch"
        assert recovered[0]["context"]["downtime_seconds"] is not None
        assert _equity_count(engine.journal) == equity_before + 1
        assert "order_send" not in mt5.call_names()
    finally:
        engine.gateway.stop()
        engine.journal.close()


def test_none_account_reinitializes_once_per_cap(tmp_path):
    """A None read shuts the session down. The cap stops a second shutdown."""
    from engine.gateway import Gateway
    from engine.journal import Journal

    journal = Journal(tmp_path / "empty.db")
    mt5 = FakeMT5()
    gateway = Gateway(
        call_timeout=0.4,
        backoff=(30, 60, 90),
        login=999999999,
        password="synthetic",
        server="MetaQuotes-Demo",
        mt5_module=mt5,
        journal=journal,
        max_account_reinitializes=1,
    )
    gateway.start()
    try:
        assert gateway.ensure_connected() is True
        mt5.account = None
        assert gateway.account_info() is None
        assert gateway.account_readable is False
        assert gateway.connected is False
        assert mt5.call_names().count("shutdown") == 1
        assert "order_send" not in mt5.call_names()

        assert gateway.account_info() is None
        assert mt5.call_names().count("shutdown") == 1
        assert mt5.call_names().count("initialize") == 1

        gateway.status.next_retry_at = 0
        assert gateway.account_info() is None
        assert mt5.call_names().count("initialize") == 2
        assert mt5.call_names().count("shutdown") == 1
        assert gateway.connected is False
        assert "order_send" not in mt5.call_names()
    finally:
        gateway.stop()
        journal.close()
