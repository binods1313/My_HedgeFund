"""A3: the System Status card never shows a false zero or an invented trade.

Covers the journal-derived fields (last trade, trades today, scan loop,
signals). A count of zero is only shown when the trades table was read.
"""

from datetime import datetime, timezone

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
    assert status["scan_loop"] == "Not built yet"
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
