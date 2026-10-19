"""TEST SIGNAL injector. PAPER only. Never run live until the operator says go."""
from __future__ import annotations

import json
import uuid
from typing import Any, Iterable

from engine.journal import Journal, utc_now_iso

TEST_TAG = "TEST SIGNAL"


class SignalInjectRefused(RuntimeError):
    """LIVE or any non-PAPER mode is refused before a journal write."""


def inject_test_signal(
    *,
    mode: str,
    journal: Journal,
    symbol: str,
    direction: str = "BUY",
    entry: float,
    stop_loss: float,
    take_profit: float | None,
    volume: float,
    expires_at: str,
    decision_id: str | None = None,
) -> str:
    """Write one tagged PENDING row. Approval and the risk gate still apply.

    Refuses before any journal or executor call unless mode is PAPER.
    """
    if str(mode).upper() != "PAPER":
        raise SignalInjectRefused(
            "TEST SIGNAL refuses LIVE before writing; PAPER only")
    did = decision_id or str(uuid.uuid4())
    journal.record_signal(
        symbol=str(symbol).upper(),
        direction=str(direction).upper(),
        score=1.0,
        bar_ts=None,
        decision_id=did,
        status="PENDING",
        strategy="test",
        timeframe="M15",
        entry_price=float(entry),
        stop_loss=float(stop_loss),
        take_profit=float(take_profit) if take_profit else None,
        volume=float(volume),
        expires_at=expires_at,
        is_test=True,
        test_tag=TEST_TAG,
    )
    journal.log_lifecycle(
        Journal.LIFECYCLE_SIGNAL_GENERATED,
        f"{TEST_TAG} {symbol} {direction}",
        decision_id=did, source="test_signal",
        context={"is_test": 1, "test_tag": TEST_TAG, "decision_id": did,
                 "ts": utc_now_iso()},
    )
    return did


def _query(source: Any, sql: str, params: Iterable[Any] = ()) -> list:
    """Journal.query or sqlite3.Connection.execute — same rows."""
    if hasattr(source, "query"):
        return list(source.query(sql, params))
    return list(source.execute(sql, tuple(params)).fetchall())


def test_decision_ids(source: Any) -> set[str]:
    """decision_id values on signals.is_test = 1."""
    return {
        str(row["decision_id"])
        for row in _query(source, "SELECT decision_id FROM signals WHERE is_test = 1")
        if row["decision_id"]
    }


def linked_decision_id(source: Any, ticket: int) -> str:
    """Latest events.decision_id for this ticket, or empty."""
    rows = _query(
        source,
        "SELECT context_json FROM events "
        "WHERE json_extract(context_json, '$.ticket') = ? "
        "ORDER BY id DESC LIMIT 8",
        (int(ticket),),
    )
    for row in rows:
        try:
            ctx = json.loads(row["context_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if ctx.get("decision_id"):
            return str(ctx["decision_id"])
    return ""


def linked_profit(source: Any, ticket: int) -> float | None:
    """Latest events.profit for this ticket, if present."""
    rows = _query(
        source,
        "SELECT context_json FROM events "
        "WHERE json_extract(context_json, '$.ticket') = ? "
        "ORDER BY id DESC LIMIT 8",
        (int(ticket),),
    )
    for row in rows:
        try:
            ctx = json.loads(row["context_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if "profit" in ctx and ctx["profit"] is not None:
            try:
                return float(ctx["profit"])
            except (TypeError, ValueError):
                continue
    return None


def is_test_ticket(source: Any, ticket: int) -> bool:
    """True when the ticket's linked decision_id is a TEST SIGNAL."""
    linked = linked_decision_id(source, ticket)
    return bool(linked) and linked in test_decision_ids(source)


def is_test_decision(journal: Journal, decision_id: str) -> bool:
    row = journal.query_one(
        "SELECT is_test FROM signals WHERE decision_id = ?", (decision_id,))
    return bool(row and int(row["is_test"] or 0) == 1)


def classify_trades(source: Any) -> list[dict[str, Any]]:
    """Every trade with is_test from the signal chain, not from ticket range."""
    test_ids = test_decision_ids(source)
    out: list[dict[str, Any]] = []
    trades = _query(
        source,
        "SELECT id, ticket, symbol, side, volume, entry_price, exit_price, "
        "status, ts FROM trades ORDER BY id",
    )
    for trade in trades:
        ticket = int(trade["ticket"])
        linked = linked_decision_id(source, ticket)
        out.append({
            "id": int(trade["id"]),
            "ticket": ticket,
            "symbol": trade["symbol"],
            "side": trade["side"],
            "volume": trade["volume"],
            "entry_price": trade["entry_price"],
            "exit_price": trade["exit_price"],
            "status": trade["status"],
            "ts": trade["ts"],
            "decision_id": linked,
            "is_test": linked in test_ids,
            "profit": linked_profit(source, ticket),
        })
    return out


def production_tickets(source: Any) -> list[int]:
    return [row["ticket"] for row in classify_trades(source) if not row["is_test"]]


def production_trade_count(journal: Journal) -> int:
    """Open+closed trades whose decision is not a TEST SIGNAL."""
    return len(production_tickets(journal))


def production_realised_profits(source: Any) -> list[float]:
    """Closed production trade profits, TEST SIGNAL excluded."""
    profits: list[float] = []
    for row in classify_trades(source):
        if row["is_test"]:
            continue
        if str(row["status"] or "").upper() != "CLOSED":
            continue
        if row["profit"] is None:
            continue
        profits.append(float(row["profit"]))
    return profits
