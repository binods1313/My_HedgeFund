"""TEST SIGNAL trades must not feed UI statistics."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from engine.journal import Journal
from engine.test_signal import (
    TEST_TAG,
    production_realised_profits,
    production_trade_count,
)
from learning.ledger import connect, link_journal
from ui import data_access
from ui.performance import max_drawdown_pct, performance_report, win_loss_ratio
from ui.stats import production_stats


def _seed(journal: Journal) -> None:
    """One production closed trade (loss) and one TEST SIGNAL closed trade (win)."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    journal.set_state("IDLE", mode="PAPER")
    journal.record_signal(
        symbol="EURUSD", direction="BUY", score=1.0, bar_ts=1792389600,
        decision_id="prod-1", status="EXECUTED", strategy="confluence",
        timeframe="M15", entry_price=1.10, stop_loss=1.09, take_profit=1.12,
        volume=0.01, expires_at=now, is_test=False,
    )
    journal.execute(
        "INSERT INTO trades (ticket, symbol, side, volume, entry_price, "
        "exit_price, status, magic, broker, ts, updated_at) "
        "VALUES (1001, 'EURUSD', 'BUY', 0.01, 1.10, 1.09, 'CLOSED', "
        "100100, 'PAPER', ?, ?)",
        (now, now),
    )
    journal.log_event(
        "trade", "paper position closed",
        context={"ticket": 1001, "profit": -1.0, "decision_id": "prod-1"},
    )
    journal.record_signal(
        symbol="EURUSD", direction="BUY", score=1.0, bar_ts=None,
        decision_id="test-1", status="EXECUTED", strategy="test",
        timeframe="M15", entry_price=1.10, stop_loss=1.09, take_profit=1.12,
        volume=0.01, expires_at=now, is_test=True, test_tag=TEST_TAG,
    )
    journal.execute(
        "INSERT INTO trades (ticket, symbol, side, volume, entry_price, "
        "exit_price, status, magic, broker, ts, updated_at) "
        "VALUES (9000002, 'EURUSD', 'BUY', 0.01, 1.10, 1.20, 'CLOSED', "
        "100100, 'PAPER', ?, ?)",
        (now, now),
    )
    journal.log_event(
        "trade", "paper position closed",
        context={"ticket": 9000002, "profit": 100.0, "decision_id": "test-1"},
    )


def test_seeded_journal_excludes_test_trade_from_every_trade_fed_stat(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "stats.db")
    _seed(journal)
    assert production_trade_count(journal) == 1
    assert production_realised_profits(journal) == [-1.0]
    journal.close()

    db = tmp_path / "stats.db"
    stats = production_stats(db)
    assert stats["readable"] is True
    assert stats["production_trade_count"] == 1
    assert stats["realised"] == [-1.0]
    assert stats["trades_today"] == 1
    assert stats["last_trade"]["ticket"] == 1001
    assert stats["sources"]["win_loss"].startswith("closed production")

    leaked = performance_report([100.0, 101.0], realised=[-1.0, 100.0])
    production = performance_report([100.0, 101.0], realised=stats["realised"])
    assert win_loss_ratio([-1.0, 100.0]) == 1.0
    assert production["win_loss"] == 0.0
    assert leaked["win_loss"] == 1.0
    assert production["max_drawdown_pct"] == max_drawdown_pct([100.0, 101.0])

    status = data_access.system_status(db)
    assert status["trades_today"] == 1
    assert status["last_trade"]["ticket"] == 1001

    learning = connect(tmp_path / "learning.db")
    linked = link_journal(learning, db)
    assert linked == 1
    ids = [int(row["journal_trade_id"]) for row in learning.execute(
        "SELECT journal_trade_id FROM trades_link")]
    assert ids == [1]
    learning.close()
