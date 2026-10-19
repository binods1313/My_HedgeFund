"""TEST SIGNAL injector. PAPER only. Never run live until the operator says go."""
from __future__ import annotations

import uuid

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


def is_test_decision(journal: Journal, decision_id: str) -> bool:
    row = journal.query_one(
        "SELECT is_test FROM signals WHERE decision_id = ?", (decision_id,))
    return bool(row and int(row["is_test"] or 0) == 1)


def production_trade_count(journal: Journal) -> int:
    """Open+closed trades whose decision is not a TEST SIGNAL."""
    test_ids = {
        row["decision_id"]
        for row in journal.query(
            "SELECT decision_id FROM signals WHERE is_test = 1")
    }
    trades = journal.query("SELECT ticket FROM trades")
    n = 0
    for trade in trades:
        ticket = int(trade["ticket"])
        rows = journal.query(
            "SELECT context_json FROM events "
            "WHERE json_extract(context_json, '$.ticket') = ? "
            "ORDER BY id DESC LIMIT 4",
            (ticket,),
        )
        linked = ""
        for row in rows:
            import json
            try:
                ctx = json.loads(row["context_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if ctx.get("decision_id"):
                linked = str(ctx["decision_id"])
                break
        if linked not in test_ids:
            n += 1
    return n
