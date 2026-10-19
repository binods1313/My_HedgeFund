"""Stage 2c approval: enqueue, PAPER fill, already-decided, no broker orders."""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from engine.commands import (
    COMMAND_APPROVE_SIGNAL,
    COMMAND_REJECT_SIGNAL,
    CommandProcessor,
)
from engine.executor import PaperExecutor
from engine.journal import Journal, STATE_IDLE, STATE_PAUSED, STATE_EMERGENCY_STOP
from engine.risk import AccountState, RiskEngine, RiskLimits
from engine.signal_approval import ApprovalContext, handle_approve, handle_reject
from engine.state import StateMachine
from engine.test_signal import TEST_TAG, SignalInjectRefused, inject_test_signal


def ticks(symbol):
    return SimpleNamespace(bid=1.09990, ask=1.10000)


def _journal(tmp_path) -> Journal:
    return Journal(tmp_path / "approve.db")


def _risk(journal) -> RiskEngine:
    risk = RiskEngine(journal, limits=RiskLimits())
    risk.set_symbol_specs({
        "EURUSD": {"trade_tick_value": 1.0, "trade_tick_size": 0.00001},
    })
    return risk


def _account() -> AccountState:
    return AccountState(
        equity=100_000.0, start_of_day_equity=100_000.0,
        peak_equity=100_000.0, open_trade_count=0, total_open_risk=0.0,
    )


def _pending(journal, *, decision_id="dec-1", expires=None, is_test=0,
             volume=0.01, entry=1.10000, sl=1.09500):
    exp = expires or (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    journal.execute(
        "INSERT INTO signals ("
        "  symbol, direction, score, bar_ts, ts, decision_id, status, "
        "  strategy, timeframe, entry_price, stop_loss, take_profit, "
        "  volume, expires_at, blocks_json, is_test, test_tag) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("EURUSD", "BUY", 0.9, 1792389600, "2026-10-19T03:15:00+00:00",
         decision_id, "PENDING", "confluence", "M15", entry, sl, 1.11000,
         volume, exp, "{}", is_test, TEST_TAG if is_test else None),
    )


def _ctx(journal, *, state=STATE_IDLE, mode="PAPER", account_ok=True):
    ex = PaperExecutor(journal=journal, tick_provider=ticks, state=state)
    return ApprovalContext(
        journal=journal, mode=mode, state=state, account_ok=account_ok,
        account=_account(), risk=_risk(journal), executor=ex,
        tick_provider=ticks,
        market_view=lambda s: {"market_open": True, "stale": False},
        decided_by="test",
    )


def test_approve_fills_paper_and_shares_the_decision_id(tmp_path):
    journal = _journal(tmp_path)
    _pending(journal)
    ctx = _ctx(journal)
    out = handle_approve(ctx, {"decision_id": "dec-1"})
    assert out.ok, out.reason
    row = journal.query_one("SELECT status FROM signals WHERE decision_id='dec-1'")
    assert row["status"] == "EXECUTED"
    trade = journal.query_one("SELECT ticket, broker, status FROM trades")
    assert trade["broker"] == "PAPER"
    assert int(trade["ticket"]) > 9_000_000
    events = journal.query(
        "SELECT context_json FROM events WHERE context_json LIKE '%dec-1%'")
    assert events
    assert journal.open_position_count() == 1


def test_second_approval_fails_already_decided(tmp_path):
    journal = _journal(tmp_path)
    _pending(journal)
    ctx = _ctx(journal)
    assert handle_approve(ctx, {"decision_id": "dec-1"}).ok
    again = handle_approve(ctx, {"decision_id": "dec-1"})
    assert again.ok is False
    assert "already decided" in again.reason
    assert journal.query_one("SELECT COUNT(*) AS n FROM trades")["n"] == 1


def test_reject_does_not_call_the_executor(tmp_path):
    journal = _journal(tmp_path)
    _pending(journal)
    ctx = _ctx(journal)
    called = []
    ctx.executor.submit = lambda *a, **k: called.append(1)
    out = handle_reject(ctx, {"decision_id": "dec-1"})
    assert out.ok
    assert journal.query_one(
        "SELECT status FROM signals")["status"] == "REJECTED"
    assert called == []
    assert journal.query_one("SELECT COUNT(*) AS n FROM trades")["n"] == 0


@pytest.mark.parametrize("state", [STATE_PAUSED, STATE_EMERGENCY_STOP])
def test_paused_or_halted_does_not_enter(tmp_path, state):
    journal = Journal(tmp_path / f"{state}.db")
    _pending(journal)
    ctx = _ctx(journal, state=state)
    out = handle_approve(ctx, {"decision_id": "dec-1"})
    assert out.ok is False
    assert journal.query_one("SELECT status FROM signals")["status"] == "PENDING"
    assert journal.query_one("SELECT COUNT(*) AS n FROM trades")["n"] == 0
    journal.close()


def test_non_paper_mode_is_refused(tmp_path):
    journal = _journal(tmp_path)
    _pending(journal)
    ctx = _ctx(journal, mode="LIVE")
    out = handle_approve(ctx, {"decision_id": "dec-1"})
    assert out.ok is False
    assert "PAPER" in out.reason
    assert journal.query_one("SELECT COUNT(*) AS n FROM trades")["n"] == 0


def test_expired_signal_is_expired_not_executed(tmp_path):
    journal = _journal(tmp_path)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    _pending(journal, expires=past)
    out = handle_approve(_ctx(journal), {"decision_id": "dec-1"})
    assert out.ok is False
    assert journal.query_one("SELECT status FROM signals")["status"] == "EXPIRED"


def test_command_processor_leaves_idle_unchanged(tmp_path):
    journal = _journal(tmp_path)
    sm = StateMachine(journal)
    sm.transition(STATE_IDLE, "ENABLE_AI")
    _pending(journal)
    ctx = _ctx(journal)
    proc = CommandProcessor(journal, sm, ctx.executor, approval_ctx=ctx)
    journal.enqueue_command(COMMAND_APPROVE_SIGNAL, {"decision_id": "dec-1"})
    proc.poll_once()
    assert sm.state == STATE_IDLE


def test_signal_approved_is_not_a_state_trigger():
    import engine.state as st
    assert "SIGNAL_APPROVED" not in inspect.getsource(st)


def test_queue_buttons_do_not_import_an_executor():
    import ui.components.signals_page as page
    src = inspect.getsource(page)
    assert "Approve" in src and "Reject" in src
    assert "queue_signal_decision" in src
    for banned in ("PaperExecutor", "submit_order", "order_send", "mt5"):
        assert banned not in src


def test_approval_module_cannot_reach_a_gateway_order():
    import engine.signal_approval as mod
    src = inspect.getsource(mod)
    for banned in ("order_send", "send_order", "LiveExecutor", "mt5",
                   "gateway.close_position", "copy_rates"):
        assert banned not in src
    assert "PaperExecutor" in src


def test_test_signal_refuses_live_before_writing(tmp_path):
    journal = _journal(tmp_path)
    with pytest.raises(SignalInjectRefused):
        inject_test_signal(
            mode="LIVE", journal=journal, symbol="EURUSD",
            entry=1.1, stop_loss=1.09, take_profit=1.12, volume=0.01,
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=20)
                        ).isoformat(),
        )
    assert journal.query_one("SELECT COUNT(*) AS n FROM signals")["n"] == 0
    assert journal.query_one("SELECT COUNT(*) AS n FROM events")["n"] == 0


def test_test_signal_writes_tagged_pending_and_still_needs_approval(tmp_path):
    journal = _journal(tmp_path)
    exp = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    did = inject_test_signal(
        mode="PAPER", journal=journal, symbol="EURUSD",
        entry=1.10000, stop_loss=1.09500, take_profit=1.11000, volume=0.01,
        expires_at=exp,
    )
    row = journal.query_one("SELECT * FROM signals WHERE decision_id = ?", (did,))
    assert row["status"] == "PENDING"
    assert int(row["is_test"]) == 1
    assert row["test_tag"] == TEST_TAG
    assert journal.query_one("SELECT COUNT(*) AS n FROM trades")["n"] == 0
    out = handle_approve(_ctx(journal), {"decision_id": did})
    assert out.ok, out.reason
    assert journal.query_one(
        "SELECT status FROM signals")["status"] == "EXECUTED"
    trade = journal.query_one("SELECT broker FROM trades")
    assert trade["broker"] == "PAPER"
    from engine.test_signal import production_trade_count
    assert production_trade_count(journal) == 0
