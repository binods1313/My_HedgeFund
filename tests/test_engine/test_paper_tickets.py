"""PAPER tickets must not collide across a process restart."""
from __future__ import annotations

from types import SimpleNamespace

from engine.executor import PaperExecutor, RiskApproval
from engine.journal import Journal, TRADE_CLOSED, utc_now_iso


def ticks(symbol):
    return SimpleNamespace(bid=1.09990, ask=1.10000)


def approve():
    return RiskApproval(approved=True, decision_id="ticket-seed", symbol="EURUSD")


def test_restarted_executor_mints_past_every_existing_ticket(tmp_path):
    """9000001 already in trades. A new process must mint a greater ticket."""
    journal = Journal(tmp_path / "tickets.db")
    now = utc_now_iso()
    journal.execute(
        "INSERT INTO trades (ticket, symbol, side, volume, entry_price, "
        "status, magic, broker, ts, updated_at) "
        "VALUES (?, 'EURUSD', 'BUY', 0.01, 1.1, ?, 100100, 'PAPER', ?, ?)",
        (9_000_001, TRADE_CLOSED, now, now),
    )
    journal.execute(
        "INSERT INTO positions (ticket, symbol, side, volume, price_open, "
        "profit, magic, updated_at) "
        "VALUES (9000005, 'GBPUSD', 'BUY', 0.01, 1.2, 0, 100100, ?)",
        (now,),
    )

    restarted = PaperExecutor(journal=journal, tick_provider=ticks)
    assert restarted._next_ticket >= 9_000_005
    fill = restarted.submit(
        restarted.prepare_order("EURUSD", "BUY", 0.01, 1.09500),
        approve(),
    )
    assert fill.ok, fill.message
    existing = {
        int(row["ticket"])
        for row in journal.query("SELECT ticket FROM trades")
    }
    existing |= {
        int(row["ticket"])
        for row in journal.query("SELECT ticket FROM positions")
    }
    existing.discard(int(fill.ticket))
    assert int(fill.ticket) > max(existing)
    assert int(fill.ticket) > 9_000_001
    journal.close()
