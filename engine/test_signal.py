# Test-only deterministic signal injector (§3 gate).
#
# PAPER ONLY. Refuses to run in LIVE. Every journal row it writes is
# labelled "TEST SIGNAL" so a reviewer can never mistake it for a live
# market signal. Produces one documented chain linked by one decision ID:
#   signal_generated -> risk_approved/rejected -> order_sent ->
#   order_filled -> order_modified (SL/TP) -> order_closed.
"""Deterministic TEST SIGNAL injector. PAPER only, never LIVE."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


TEST_TAG = "TEST SIGNAL"


@dataclass(frozen=True)
class TestSignalResult:
    decision_id: str
    ticket: int | None
    risk_approved: bool
    closed: bool


def inject_test_signal(*, journal, risk_engine, executor, symbol: str,
                       side: str = "BUY", entry: float = 1.0,
                       stop_loss: float = 0.99,
                       take_profit: float = 1.02,
                       volume: float = 0.01,
                       mode: str = "PAPER") -> TestSignalResult:
    """Run one deterministic signal through risk -> PAPER submit -> close.

    Raises in LIVE mode. Writes lifecycle rows, all tagged TEST SIGNAL.
    """
    from engine.executor import RiskApproval
    from engine.risk import OrderIntent

    if str(mode or "").upper() != "PAPER":
        raise RuntimeError(
            f"{TEST_TAG}: refused in mode {mode!r}; PAPER only.")

    decision_id = f"TEST-{uuid.uuid4().hex[:8]}"
    journal.log_lifecycle(
        journal.LIFECYCLE_SIGNAL_GENERATED,
        f"{TEST_TAG} generated {symbol} {side}",
        decision_id=decision_id, source="test_hook",
        context={"symbol": symbol, "side": side, "test": True})

    intent = OrderIntent(
        symbol=symbol, side=side, volume=volume,
        entry_price=entry, stop_loss=stop_loss,
        tick_value=1.0, tick_size=0.00001)
    from engine.risk import AccountState
    account = AccountState(
        equity=10_000.0, start_of_day_equity=10_000.0,
        peak_equity=10_000.0, open_trade_count=0,
        total_open_risk=0.0, unprotected_positions=0)
    decision = risk_engine.evaluate(intent, account)
    journal.record_risk_decision(symbol, bool(decision.approved),
                                 list(decision.violations or decision.reasons))
    if decision.approved:
        journal.log_lifecycle(
            journal.LIFECYCLE_RISK_APPROVED,
            f"{TEST_TAG} risk approved {symbol}",
            decision_id=decision_id, source="test_hook",
            context={"symbol": symbol})
    else:
        journal.log_lifecycle(
            journal.LIFECYCLE_RISK_REJECTED,
            f"{TEST_TAG} risk rejected {symbol}: "
            f"{decision.reason_text()}",
            decision_id=decision_id, source="test_hook",
            context={"symbol": symbol,
                     "reason": decision.reason_text()})
        return TestSignalResult(decision_id, None, False, False)

    approval = RiskApproval(approved=True, decision_id=decision_id,
                            symbol=symbol)
    request = executor.prepare_order(
        symbol, side, volume, stop_loss, take_profit,
        comment=f"{TEST_TAG} {decision_id}")
    result = executor.submit(request, approval)
    if not result.ok:
        return TestSignalResult(decision_id, None, True, False)

    journal.log_lifecycle(
        journal.LIFECYCLE_ORDER_MODIFIED,
        f"{TEST_TAG} SL/TP set ticket={result.ticket}",
        decision_id=decision_id, source="test_hook",
        context={"ticket": result.ticket})
    close_result = executor.close(result.ticket)
    if close_result.ok:
        journal.log_lifecycle(
            journal.LIFECYCLE_ORDER_CLOSED,
            f"{TEST_TAG} position closed ticket={result.ticket}",
            decision_id=decision_id, source="test_hook",
            context={"ticket": result.ticket})
    return TestSignalResult(decision_id, result.ticket, True,
                            bool(close_result.ok))
