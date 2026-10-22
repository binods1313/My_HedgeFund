"""A3: the System Status card never shows a false zero or an invented trade.

Covers the journal-derived fields (last trade, trades today, scan loop,
signals). A count of zero is only shown when the trades table was read.
"""

from datetime import datetime, timedelta, timezone

import pytest

from engine.journal import Journal
from ui import data_access


def _journal(tmp_path):
    return Journal(tmp_path / "card.db")


def test_unreadable_journal_reports_trades_today_unknown_not_zero(tmp_path):
    # An absent journal is "not read": trades_today must be None, not 0.
    status = data_access.system_status(tmp_path / "absent.db")
    assert status["trades_today"] is None


def test_empty_trades_table_reports_a_real_zero(tmp_path):
    j = _journal(tmp_path)
    j.set_state("OFF", mode="PAPER")
    j.close()
    status = data_access.system_status(tmp_path / "card.db")
    assert status["trades_today"] == 0
    assert status["last_trade"] is None


def test_last_trade_is_none_when_no_trade_rows(tmp_path):
    j = _journal(tmp_path)
    j.set_state("OFF", mode="PAPER")
    j.close()
    assert data_access.system_status(tmp_path / "card.db")["last_trade"] is None


def test_scan_loop_and_signals_state_is_stated_not_implied(tmp_path):
    status = data_access.system_status(tmp_path / "absent.db")
    assert status["scan_running"] is None
    assert status["last_cycle_ts"] is None
    assert status["symbols_scanned"] is None
    assert "Not built yet" not in str(status["scan_loop"])
    assert status["signals"] == "No signals yet"


def test_trades_today_counts_only_todays_rows(tmp_path):
    j = _journal(tmp_path)
    j.set_state("OFF", mode="PAPER")
    today = datetime.now(timezone.utc).isoformat(timespec="seconds")
    yesterday = "2000-01-01T00:00:00+00:00"
    ticket = 1
    for ts in (today, today, yesterday):
        j.execute(
            "INSERT INTO trades (ts, updated_at, ticket, symbol, side, volume, "
            "entry_price, exit_price, status) "
            "VALUES (?, ?, ?, 'EURUSD', 'BUY', 0.01, 1.0, NULL, 'OPEN')",
            (ts, ts, ticket),
        )
        ticket += 1
    j.close()
    status = data_access.system_status(tmp_path / "card.db")
    assert status["trades_today"] == 2


def _beat(j, ts):
    j.execute(
        "INSERT INTO heartbeats (state, pid, connected, ts) "
        "VALUES ('IDLE', 40964, 1, ?)",
        (ts,),
    )


def test_heartbeat_gap_is_reported_when_the_host_slept(tmp_path):
    """A 40-minute hole in the beats is a suspension and must be visible."""
    j = _journal(tmp_path)
    j.set_state("IDLE", mode="PAPER")
    now = datetime.now(timezone.utc)
    for minutes in (50, 10):
        _beat(j, (now - timedelta(minutes=minutes)).isoformat(timespec="seconds"))
    j.close()

    status = data_access.system_status(tmp_path / "card.db")
    assert status["heartbeat_gap_count"] == 1
    assert status["heartbeat_gap_max_s"] == pytest.approx(2400.0, abs=5.0)


def test_no_heartbeat_gaps_reports_a_real_zero(tmp_path):
    j = _journal(tmp_path)
    j.set_state("IDLE", mode="PAPER")
    now = datetime.now(timezone.utc)
    for seconds in (0, 1, 2):
        _beat(j, (now - timedelta(seconds=seconds)).isoformat(timespec="seconds"))
    j.close()

    status = data_access.system_status(tmp_path / "card.db")
    assert status["heartbeat_gap_count"] == 0
    assert status["heartbeat_gap_max_s"] == 0.0


def test_unread_journal_reports_gaps_not_read_not_zero(tmp_path):
    """An absent journal means the probe did not run. Not "no gaps"."""
    status = data_access.system_status(tmp_path / "absent.db")
    assert status["heartbeat_gap_count"] is None
    assert status["heartbeat_gap_max_s"] is None
