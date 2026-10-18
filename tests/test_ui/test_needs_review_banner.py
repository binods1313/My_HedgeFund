"""Tests for the needs-review banner filtering.

The banner in ui/components/header.py must only fire for signals with status
NEEDS_REVIEW (crash-recovery decisions), not for ordinary PENDING signals.
PENDING is the expected state of a signal waiting for manual approval under
the ``approve_first`` execution mode — it is NOT a crash-recovery condition.
"""
from __future__ import annotations

from engine.journal import Journal
from ui import data_access


def _journal(tmp_path) -> Journal:
    return Journal(tmp_path / "needs_review.db")


def _insert_signal(j: Journal, *, symbol: str, status: str, decision_id: str):
    """Insert a signal row with a valid v4 schema."""
    j.execute(
        "INSERT INTO signals ("
        "  symbol, direction, score, bar_ts, ts, decision_id, status, "
        "  strategy, timeframe, entry_price, stop_loss, take_profit, "
        "  volume, expires_at, decided_by, decided_at, blocks_json, "
        "  reject_reason, superseded_by_decision_id, is_test, test_tag) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (symbol, "BUY", 0.9, 1000, "2026-10-18T13:00:00+00:00",
         decision_id, status,
         "confluence", "M15", 1.1000, 1.0950, 1.1050,
         0.01, "2026-10-18T13:15:00+00:00",
         None, None, "{}", None, None, 0, None),
    )


def test_empty_journal_no_needs_review(tmp_path):
    """With no signals at all, pending_signals must be 0, not None."""
    j = _journal(tmp_path)
    j.set_state("OFF", mode="PAPER")
    j.close()
    status = data_access.system_status(tmp_path / "needs_review.db")
    assert status["pending_signals"] == 0


def test_ordinary_pending_does_not_trigger_banner(tmp_path):
    """A PENDING signal (normal approve_first wait) must NOT be counted as
    needs-review. The banner checks pending_signals > 0."""
    j = _journal(tmp_path)
    j.set_state("OFF", mode="PAPER")
    _insert_signal(j, symbol="EURUSD", status="PENDING",
                   decision_id="dec-pending-1")
    j.close()
    status = data_access.system_status(tmp_path / "needs_review.db")
    # The count queries NEEDS_REVIEW, not PENDING
    assert status["pending_signals"] == 0


def test_needs_review_signal_is_counted(tmp_path):
    """A NEEDS_REVIEW signal (crash recovery) must be counted."""
    j = _journal(tmp_path)
    j.set_state("OFF", mode="PAPER")
    _insert_signal(j, symbol="EURUSD", status="NEEDS_REVIEW",
                   decision_id="dec-crash-1")
    j.close()
    status = data_access.system_status(tmp_path / "needs_review.db")
    assert status["pending_signals"] == 1


def test_both_pending_and_needs_review_counted_correctly(tmp_path):
    """PENDING is not needs-review; only NEEDS_REVIEW is counted."""
    j = _journal(tmp_path)
    j.set_state("OFF", mode="PAPER")
    _insert_signal(j, symbol="EURUSD", status="PENDING",
                   decision_id="dec-pending-1")
    _insert_signal(j, symbol="GBPUSD", status="NEEDS_REVIEW",
                   decision_id="dec-crash-1")
    j.close()
    status = data_access.system_status(tmp_path / "needs_review.db")
    assert status["pending_signals"] == 1  # only NEEDS_REVIEW


def test_multiple_needs_review_all_counted(tmp_path):
    """Multiple crash-recovery decisions all appear."""
    j = _journal(tmp_path)
    j.set_state("OFF", mode="PAPER")
    _insert_signal(j, symbol="EURUSD", status="NEEDS_REVIEW",
                   decision_id="dec-crash-1")
    _insert_signal(j, symbol="GBPUSD", status="NEEDS_REVIEW",
                   decision_id="dec-crash-2")
    _insert_signal(j, symbol="USDJPY", status="NEEDS_REVIEW",
                   decision_id="dec-crash-3")
    j.close()
    status = data_access.system_status(tmp_path / "needs_review.db")
    assert status["pending_signals"] == 3
