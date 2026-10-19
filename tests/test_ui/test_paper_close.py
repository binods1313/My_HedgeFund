"""PAPER close goes through PaperExecutor. A paper ticket never hits MT5."""
from __future__ import annotations

import json
from engine.executor import PaperExecutor
from engine.journal import Journal
from ui import data_access
from ui.components import positions_matrix
from tests.test_ui.test_data_access import _stub_market


def _broker_spy(monkeypatch):
    calls: list[int] = []

    def fake_broker_close(ticket: int):
        calls.append(int(ticket))
        return {"success": True, "status": "CLOSED", "message": "mt5 closed"}

    monkeypatch.setattr(data_access, "broker_close_position", fake_broker_close)
    return calls


def test_paper_ticket_closes_and_journals(monkeypatch, tmp_path) -> None:
    _stub_market(monkeypatch, tmp_path)
    broker_calls = _broker_spy(monkeypatch)

    filled = data_access.submit_order("EURUSD", "BUY", 0.10, 30, 60)
    assert filled.ok, filled.message
    ticket = int(filled.ticket)
    assert ticket > 9_000_000

    closed = data_access.close_position(ticket)
    assert closed.ok, closed.message
    assert closed.ticket == ticket
    assert broker_calls == []

    journal = Journal(tmp_path / "engine.db", migrate=False)
    try:
        trades = journal.query(
            "SELECT ticket, broker, status, exit_price FROM trades WHERE ticket = ?",
            (ticket,),
        )
        assert len(trades) == 1
        assert trades[0]["broker"] == "PAPER"
        assert trades[0]["status"] == "CLOSED"
        assert trades[0]["exit_price"] is not None
        assert journal.open_position_count() == 0

        events = journal.query(
            "SELECT message, context_json FROM events "
            "WHERE message = 'paper position closed' ORDER BY id DESC LIMIT 1")
        assert events
        ctx = json.loads(events[0]["context_json"])
        assert ctx["ticket"] == ticket
        assert ctx["reason"] == "MANUAL"
        assert "profit" in ctx
        assert "price" in ctx
        assert ctx.get("decision_id")
    finally:
        journal.close()


def test_paper_ticket_cannot_reach_order_send(monkeypatch, tmp_path) -> None:
    _stub_market(monkeypatch, tmp_path)
    broker_calls = _broker_spy(monkeypatch)

    filled = data_access.submit_order("EURUSD", "BUY", 0.10, 30, 60)
    assert filled.ok, filled.message

    def boom(ticket):
        broker_calls.append(int(ticket))
        raise AssertionError("paper ticket must not reach mt5.order_send")

    monkeypatch.setattr(data_access, "broker_close_position", boom)
    closed = data_access.close_position(int(filled.ticket))
    assert closed.ok, closed.message
    assert broker_calls == []


def test_non_paper_ticket_uses_the_live_close_path(monkeypatch, tmp_path) -> None:
    _stub_market(monkeypatch, tmp_path)
    broker_calls = _broker_spy(monkeypatch)
    paper_closes: list[int] = []
    real_close = PaperExecutor.close

    def spy_close(self, ticket, *args, **kwargs):
        paper_closes.append(int(ticket))
        return real_close(self, ticket, *args, **kwargs)

    monkeypatch.setattr(PaperExecutor, "close", spy_close)

    result = data_access.close_position(700001)
    assert result.ok, result.message
    assert broker_calls == [700001]
    assert paper_closes == []


def test_close_is_refused_while_the_account_mismatches(monkeypatch, tmp_path) -> None:
    _stub_market(monkeypatch, tmp_path)
    broker_calls = _broker_spy(monkeypatch)
    monkeypatch.setattr(
        data_access, "_journal_account_matches",
        lambda: data_access.Result(
            False, "ACCOUNT_MISMATCH", "Account mismatch; order not submitted."),
    )
    result = data_access.close_position(9_000_001)
    assert result.ok is False
    assert result.code == "ACCOUNT_MISMATCH"
    assert broker_calls == []


def test_foreign_magic_has_no_close_button() -> None:
    body = open(positions_matrix.__file__, encoding="utf-8").read()
    assert 'if not row.get("managed"):' in body
    assert 'key=f"close_{int(ticket)}"' in body
