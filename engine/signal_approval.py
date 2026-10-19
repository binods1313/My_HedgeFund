"""Stage 2c: APPROVE_SIGNAL / REJECT_SIGNAL between scan cycles.

This module imports ``PaperExecutor`` only. It does not import a gateway
order function, a live executor class, or the broker package. The engine
claims one command, this handler runs, and the state machine is not moved.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from engine.executor import ExecutionRejected, PaperExecutor, RiskApproval
from engine.journal import (
    STATE_EMERGENCY_STOP,
    STATE_PAUSED,
    Journal,
    utc_now_iso,
)
from engine.commands import CommandOutcome
from engine.journal import CMD_DONE, CMD_FAILED

_DRIFT_FRACTION = 0.5


@dataclass
class ApprovalContext:
    """What the handler needs. Built by the engine or a test."""

    journal: Journal
    mode: str
    state: str
    account_ok: bool
    account: Any
    risk: Any
    executor: Any
    tick_provider: Callable[[str], Any]
    market_view: Callable[[str], dict]
    decided_by: str = "engine"

    @classmethod
    def from_engine(cls, engine) -> "ApprovalContext":
        gateway = getattr(engine, "gateway", None)

        def tick(symbol: str):
            if gateway is None:
                return None
            try:
                return gateway.symbol_info_tick(symbol)
            except Exception:  # noqa: BLE001
                return None

        def view(symbol: str) -> dict:
            if gateway is None:
                return {}
            try:
                snap = gateway.snapshot([symbol])
            except Exception:  # noqa: BLE001
                return {}
            views = snap.get("symbols", snap) if isinstance(snap, dict) else {}
            found = views.get(symbol, {}) if isinstance(views, dict) else {}
            return found if isinstance(found, dict) else {}

        return cls(
            journal=engine.journal,
            mode=str(getattr(getattr(engine, "config", None), "mode", "PAPER")),
            state=str(getattr(engine.state, "state", "")),
            account_ok=bool(getattr(engine, "_account_verified", False)),
            account=getattr(engine, "account", None),
            risk=getattr(engine, "risk", None),
            executor=getattr(engine, "executor", None),
            tick_provider=tick,
            market_view=view,
            decided_by="engine",
        )


def handle_approve(ctx: ApprovalContext, params: dict) -> CommandOutcome:
    """Approve one PENDING signal through the PAPER executor."""
    return _handle(ctx, params, approve=True)


def handle_reject(ctx: ApprovalContext, params: dict) -> CommandOutcome:
    """Operator rejection. No executor call."""
    return _handle(ctx, params, approve=False)


def _handle(ctx: ApprovalContext, params: dict, *, approve: bool) -> CommandOutcome:
    decision_id = str((params or {}).get("decision_id") or "").strip()
    if not decision_id:
        return _fail("APPROVE_SIGNAL/REJECT_SIGNAL requires decision_id")

    row = ctx.journal.query_one(
        "SELECT * FROM signals WHERE decision_id = ?", (decision_id,))
    if row is None:
        return _fail("unknown decision_id", decision_id=decision_id)

    if row["status"] != "PENDING":
        return _fail("already decided", decision_id=decision_id)

    if not approve:
        _finish_signal(ctx.journal, decision_id, "REJECTED",
                       ctx.decided_by, "operator rejected")
        return CommandOutcome(ok=True, status=CMD_DONE,
                              result="rejected", reason="")

    blocked = _entry_blocked(ctx)
    if blocked:
        return _fail(blocked, decision_id=decision_id)

    now = datetime.now(timezone.utc)
    expires = row["expires_at"]
    if expires:
        exp = datetime.fromisoformat(str(expires))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= now:
            _finish_signal(ctx.journal, decision_id, "EXPIRED",
                           ctx.decided_by, "expired")
            return _fail("expired", decision_id=decision_id)

    symbol = str(row["symbol"])
    direction = str(row["direction"])
    entry = float(row["entry_price"] or 0.0)
    stop = float(row["stop_loss"] or 0.0)
    volume = float(row["volume"] or 0.0)
    take_profit = row["take_profit"]
    view = ctx.market_view(symbol) or {}
    if not view.get("market_open", False):
        return _fail("market closed", decision_id=decision_id)
    if view.get("stale"):
        return _fail("stale quote", decision_id=decision_id)

    tick = ctx.tick_provider(symbol)
    if tick is None:
        return _fail("no quote", decision_id=decision_id)
    quote = float(tick.ask if direction == "BUY" else tick.bid)
    if stop > 0 and entry > 0:
        threshold = _DRIFT_FRACTION * abs(entry - stop)
        if threshold > 0 and abs(quote - entry) > threshold:
            _finish_signal(ctx.journal, decision_id, "EXPIRED",
                           ctx.decided_by, "drift")
            return _fail("drift", decision_id=decision_id)

    if ctx.risk is None or ctx.account is None:
        return _fail("risk gate unavailable", decision_id=decision_id)
    try:
        intent = ctx.risk.build_order_intent(
            symbol=symbol, side=direction, volume=volume,
            entry_price=quote, stop_loss=stop,
        )
        decision = ctx.risk.evaluate(intent, ctx.account)
    except Exception as exc:  # noqa: BLE001
        return _fail(f"risk gate: {exc}", decision_id=decision_id)
    if not decision.approved:
        reason = "; ".join(getattr(decision, "violations", ()) or ()) or "risk"
        _finish_signal(ctx.journal, decision_id, "REJECTED",
                       ctx.decided_by, reason)
        return _fail(reason, decision_id=decision_id)

    if not isinstance(ctx.executor, PaperExecutor):
        return _fail("PAPER executor required", decision_id=decision_id)

    claimed = ctx.journal.execute(
        "UPDATE signals SET status = 'APPROVED', decided_by = ?, decided_at = ? "
        "WHERE decision_id = ? AND status = 'PENDING'",
        (ctx.decided_by, utc_now_iso(), decision_id),
    )
    if getattr(claimed, "rowcount", 1) == 0:
        return _fail("already decided", decision_id=decision_id)

    approval = RiskApproval(
        approved=True, decision_id=decision_id, symbol=symbol)
    comment = "TEST SIGNAL" if int(row["is_test"] or 0) == 1 else "paper"
    try:
        request = ctx.executor.prepare_order(
            symbol, direction, volume, stop,
            float(take_profit) if take_profit else None,
            comment=comment,
        )
        result = ctx.executor.submit(request, approval)
    except ExecutionRejected as exc:
        _finish_signal(ctx.journal, decision_id, "FAILED",
                       ctx.decided_by, str(exc))
        return _fail(str(exc), decision_id=decision_id)
    except Exception as exc:  # noqa: BLE001
        _finish_signal(ctx.journal, decision_id, "FAILED",
                       ctx.decided_by, str(exc))
        return _fail(str(exc), decision_id=decision_id)

    if not result.ok:
        _finish_signal(ctx.journal, decision_id, "FAILED",
                       ctx.decided_by, result.message)
        return _fail(result.message, decision_id=decision_id)

    ctx.journal.execute(
        "UPDATE signals SET status = 'EXECUTED', decided_by = ?, decided_at = ? "
        "WHERE decision_id = ?",
        (ctx.decided_by, utc_now_iso(), decision_id),
    )
    ctx.journal.log_lifecycle(
        Journal.LIFECYCLE_ORDER_FILLED,
        f"{symbol} executed ticket={result.ticket}",
        decision_id=decision_id, source="approval",
        context={"ticket": result.ticket, "decision_id": decision_id},
    )
    return CommandOutcome(ok=True, status=CMD_DONE,
                          result=f"executed ticket={result.ticket}", reason="")


def _entry_blocked(ctx: ApprovalContext) -> str:
    if str(ctx.mode).upper() != "PAPER":
        return "mode is not PAPER"
    if ctx.state in (STATE_PAUSED, STATE_EMERGENCY_STOP, "PAUSED", "EMERGENCY_STOP"):
        return f"engine is {ctx.state}"
    if not ctx.account_ok:
        return "account guard blocked"
    return ""


def _finish_signal(journal: Journal, decision_id: str, status: str,
                   decided_by: str, reason: str) -> None:
    journal.execute(
        "UPDATE signals SET status = ?, decided_by = ?, decided_at = ?, "
        "reject_reason = ? WHERE decision_id = ? AND status = 'PENDING'",
        (status, decided_by, utc_now_iso(), reason, decision_id),
    )


def _fail(reason: str, *, decision_id: str = "") -> CommandOutcome:
    return CommandOutcome(ok=False, status=CMD_FAILED, reason=reason,
                          result=decision_id)
