# Reality-check gate tests (§2b, §3).
"""Gate proofs: reconcile survival, TEST SIGNAL chain, single lock."""

from __future__ import annotations

import pytest

from engine.executor import PaperExecutor, RiskApproval
from engine.journal import Journal
from engine.risk import AccountState, OrderIntent, RiskEngine
from engine.test_signal import TEST_TAG, inject_test_signal


class StubTick:
    def __init__(self, bid=1.10000, ask=1.10002):
        self.bid = bid
        self.ask = ask


def _ticks(symbol):
    return StubTick()


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "gate.db")
    yield j
    j.close()


def _intent(**over):
    data = dict(symbol="EURUSD", side="BUY", volume=0.01,
                entry_price=1.10000, stop_loss=1.09500,
                tick_value=1.0, tick_size=0.00001)
    data.update(over)
    return OrderIntent(**data)


def _account(**over):
    data = dict(equity=100_000.0, start_of_day_equity=100_000.0,
                peak_equity=100_000.0, open_trade_count=0,
                total_open_risk=0.0, unprotected_positions=0)
    data.update(over)
    return AccountState(**data)


def test_paper_positions_survive_reconcile(journal):
    """Simulated fills are journal rows, not broker rows."""
    from engine.__main__ import Engine
    from engine.config import EngineConfig, EngineSettings
    from engine.state import StateMachine

    executor = PaperExecutor(journal=journal, tick_provider=_ticks)
    risk = RiskEngine(journal)
    decision = risk.evaluate(_intent(), _account())
    assert decision.approved
    approval = RiskApproval.from_decision(decision, "EURUSD")
    request = executor.prepare_order("EURUSD", "BUY", 0.01, 1.09500,
                                     1.10500, comment="gate proof")
    result = executor.submit(request, approval)
    assert result.ok
    assert journal.positions()

    class EmptyGateway:
        connected = True

        def ensure_connected(self):
            return True

        def positions_get(self):
            return []

        status = type("S", (), {"last_error": ""})()

    config = EngineConfig(mode="PAPER", live_confirmation_required=True,
                          engine=EngineSettings(), risk=None)
    engine = Engine(config=config, journal=journal,
                    state=StateMachine(journal), gateway=EmptyGateway(),
                    executor=executor, commands=None, risk=risk,
                    guard=None, _account_verified=True)
    outcome = engine._reconcile_positions_unlocked()
    assert outcome["adopted"] == [] and outcome["vanished"] == []
    assert journal.positions(), (
        "reconcile must not delete PAPER rows")


def test_test_signal_chain_links_one_decision_id(journal):
    risk = RiskEngine(journal)
    executor = PaperExecutor(journal=journal, tick_provider=_ticks)
    out = inject_test_signal(journal=journal, risk_engine=risk,
                             executor=executor, symbol="EURUSD")
    assert out.risk_approved and out.ticket and out.closed
    chain = journal.lifecycle_chain(out.decision_id)
    kinds = [row["category"] for row in chain]
    for expected in ("signal_generated", "risk_approved", "order_sent",
                     "order_filled", "order_modified", "order_closed"):
        assert expected in kinds, f"missing {expected} in {kinds}"


def test_test_signal_refuses_live(journal):
    risk = RiskEngine(journal)
    executor = PaperExecutor(journal=journal, tick_provider=_ticks)
    with pytest.raises(RuntimeError, match="PAPER only"):
        inject_test_signal(journal=journal, risk_engine=risk,
                           executor=executor, symbol="EURUSD",
                           mode="LIVE")


def test_single_instance_lock_refuses_second_engine(tmp_path):
    from engine.__main__ import (
        _acquire_single_instance_lock,
        _release_single_instance_lock,
    )
    from engine.config import EngineConfig, EngineSettings
    from engine.journal import resolve_db_path

    db = tmp_path / "single.db"
    Journal(db).close()
    kwargs = dict(mode="PAPER", live_confirmation_required=True,
                  risk=None)
    first_cfg = EngineConfig(
        engine=EngineSettings(db_path=str(resolve_db_path(str(db)))),
        **kwargs)
    second_cfg = EngineConfig(
        engine=EngineSettings(db_path=str(resolve_db_path(str(db)))),
        **kwargs)
    first = _acquire_single_instance_lock(first_cfg)
    assert first is not None
    try:
        second = _acquire_single_instance_lock(second_cfg)
        assert second is None, "second engine must be refused"
    finally:
        _release_single_instance_lock(first_cfg)
        try:
            first.close()
        except OSError:
            pass


def test_lifecycle_rejects_unknown_category(journal):
    with pytest.raises(ValueError, match="unknown lifecycle"):
        journal.log_lifecycle("not_a_stage", "x", decision_id="TEST-1")
