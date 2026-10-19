"""Position monitor: SL/TP every loop; both hit journals AMBIGUOUS_SL_TP."""
from __future__ import annotations

from types import SimpleNamespace

from engine.executor import PaperExecutor, RiskApproval
from engine.journal import Journal
import json


def _ticks(bid, ask):
    def provider(symbol):
        return SimpleNamespace(bid=bid, ask=ask)
    return provider


def test_ambiguous_sl_tp_closes_adverse_and_journals(tmp_path):
    journal = Journal(tmp_path / "mon.db")
    ex = PaperExecutor(journal=journal, tick_provider=_ticks(1.10000, 1.10002))
    fill = ex.submit(
        ex.prepare_order("EURUSD", "BUY", 0.01, 1.09950, 1.10050),
        RiskApproval(approved=True, decision_id="mon-1", symbol="EURUSD"),
    )
    assert fill.ok
    # A gap print that is both through SL and TP.
    ex.tick_provider = _ticks(1.09800, 1.10100)
    closed = ex.on_tick("EURUSD")
    assert len(closed) == 1
    assert closed[0]["reason"] == "AMBIGUOUS_SL_TP"
    event = journal.query_one(
        "SELECT context_json FROM events WHERE message = 'paper position closed'")
    ctx = json.loads(event["context_json"])
    assert ctx["reason"] == "AMBIGUOUS_SL_TP"
    assert journal.open_position_count() == 0
    journal.close()


def test_monitor_runs_from_hydrated_journal(tmp_path):
    journal = Journal(tmp_path / "hyd.db")
    first = PaperExecutor(journal=journal, tick_provider=_ticks(1.10000, 1.10002))
    first.submit(
        first.prepare_order("EURUSD", "BUY", 0.01, 1.09900),
        RiskApproval(approved=True, decision_id="hyd-1", symbol="EURUSD"),
    )
    second = PaperExecutor(journal=journal, tick_provider=_ticks(1.09800, 1.09802))
    second.hydrate_from_journal()
    closed = second.on_tick("EURUSD")
    assert closed
    journal.close()
