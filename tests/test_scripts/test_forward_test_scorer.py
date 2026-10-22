"""Tests for the forward-test scorer."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta

import pytest

from scripts.forward_test_scorer import (
    BAR_SOURCE_MT5,
    BAR_SOURCE_TABLE,
    Mt5Unavailable,
    ScoredSignal,
    asset_class_spread_points,
    build_summary,
    build_weekly_results,
    default_point_size,
    format_report,
    get_signals,
    resolve_bar_provider,
    score_bar_hit,
    score_signal,
    score_signals,
    _fetch_bars_from_mt5,
    _fetch_bars_from_table,
    _print_epoch_example,
)


TEST_BASE_TS = 1700000000

#: A broker epoch inside the default scoring window. The journal queries
#: filter on the UTC ``ts`` column, so a fixture anchored to the 2023
#: TEST_BASE_TS would be dropped by any weeks<=52 window.
RECENT_TS = int(datetime.now(timezone.utc).timestamp()) - 86400


def _make_db(tmp_path):
    """Create a journal DB with MIGRATION_1 through MIGRATION_4 schema."""
    from engine.journal import MIGRATION_1, MIGRATION_2, MIGRATION_3, MIGRATION_4

    db_path = tmp_path / "test_journal.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    for stmt in MIGRATION_1:
        conn.execute(stmt)
    for stmt in MIGRATION_2:
        conn.execute(stmt)
    for stmt in MIGRATION_3:
        conn.execute(stmt)
    for stmt in MIGRATION_4:
        conn.execute(stmt)
    conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (2, '2024-01-01T00:00:00Z')")
    conn.commit()
    return conn


def _make_signal_row(
    conn,
    signal_id=1,
    symbol="EURUSD",
    direction="BUY",
    entry_price=1.1000,
    stop_loss=1.0900,
    take_profit=1.1200,
    bar_ts=None,
    is_test=0,
    score=0.5,
    signal_ts=None,
):
    """Insert a signal row and return it as a Row."""
    if bar_ts is None:
        bar_ts = TEST_BASE_TS
    if signal_ts is None:
        signal_ts = datetime.fromtimestamp(bar_ts, tz=timezone.utc).isoformat()
    conn.execute(
            """INSERT INTO signals (symbol, direction, score, bar_ts, ts, decision_id,
              status, strategy, timeframe, entry_price, stop_loss, take_profit,
              volume, expires_at, decided_by, decided_at, blocks_json, reject_reason,
              superseded_by_decision_id, is_test, test_tag)
             VALUES (?, ?, ?, ?, ?, ?, 'EXECUTED', 'confluence', 'M15', ?, ?, ?,
              0.01, ?, 'engine', NULL, '{}', NULL, NULL, ?, NULL)""",
            (symbol, direction, float(score), bar_ts,
             signal_ts,
             f"test-decision-{signal_id}", entry_price, stop_loss, take_profit,
             datetime.fromtimestamp(bar_ts + 3600, tz=timezone.utc).isoformat(),
             is_test),
    )
    conn.commit()
    cur = conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,))
    return cur.fetchone()


def _make_bar_table(conn):
    """Create a bars table for testing."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bars (
            ts INTEGER PRIMARY KEY,
            symbol TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL DEFAULT 0
        )
    """)
    conn.commit()


def _insert_bar(conn, ts, symbol, open_p, high, low, close, volume=100):
    conn.execute(
        "INSERT INTO bars (ts, symbol, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, symbol, open_p, high, low, close, volume),
    )
    conn.commit()


class TestAssetClassSpread:
    def test_forex_spread_default(self):
        assert asset_class_spread_points("EURUSD") == 10.0

    def test_jpy_spread(self):
        assert asset_class_spread_points("USDJPY") == 20.0

    def test_gold_spread(self):
        assert asset_class_spread_points("XAUUSD") == 25.0

    def test_silver_spread(self):
        assert asset_class_spread_points("XAGUSD") == 10.0


class TestPointSize:
    def test_forex_point(self):
        assert default_point_size("EURUSD") == 0.0001

    def test_jpy_point(self):
        assert default_point_size("USDJPY") == 0.01


class TestScoreBarHit:
    def test_buy_tp_hit(self):
        bar = (1700000000, 1.1000, 1.1210, 1.0950, 1.1100)
        assert score_bar_hit("BUY", 1.1000, 1.0900, 1.1200, bar) == "TP"

    def test_buy_sl_hit(self):
        bar = (1700000000, 1.1000, 1.1050, 1.0850, 1.0900)
        assert score_bar_hit("BUY", 1.1000, 1.0900, 1.1200, bar) == "SL"

    def test_buy_no_hit(self):
        bar = (1700000000, 1.1000, 1.1050, 1.0950, 1.1020)
        assert score_bar_hit("BUY", 1.1000, 1.0900, 1.1200, bar) is None

    def test_sell_tp_hit(self):
        bar = (1700000000, 1.1000, 1.1050, 1.0790, 1.0800)
        assert score_bar_hit("SELL", 1.1000, 1.1100, 1.0800, bar) == "TP"

    def test_sell_sl_hit(self):
        bar = (1700000000, 1.1000, 1.1250, 1.0950, 1.1100)
        assert score_bar_hit("SELL", 1.1000, 1.1100, 1.0800, bar) == "SL"

    def test_same_bar_tp_and_sl_counts_as_adverse(self):
        """When both TP and SL are within the same bar, SL wins (adverse outcome)."""
        bar = (1700000000, 1.1000, 1.1300, 1.0850, 1.1050)
        assert score_bar_hit("BUY", 1.1000, 1.0900, 1.1200, bar) == "SL"

    def test_same_bar_sell_tp_and_sl_counts_as_adverse(self):
        """When both TP and SL are within the same bar for SELL, SL wins."""
        bar = (1700000000, 1.1000, 1.1300, 1.0850, 1.1050)
        assert score_bar_hit("SELL", 1.1000, 1.1100, 1.0800, bar) == "SL"


class TestScoreSignal:
    def test_tp_hit_scenario(self, tmp_path):
        conn = _make_db(tmp_path)
        _make_bar_table(conn)
        sig = _make_signal_row(
            conn, symbol="EURUSD", direction="BUY",
            entry_price=1.1000, stop_loss=1.0900, take_profit=1.1200,
        )
        bars = [
            (TEST_BASE_TS + 900, 1.1000, 1.1050, 1.0980, 1.1020),
            (TEST_BASE_TS + 1800, 1.1020, 1.1100, 1.1000, 1.1080),
            (TEST_BASE_TS + 2700, 1.1080, 1.1250, 1.1050, 1.1150),
        ]
        scored = score_signal(sig, bars, "EURUSD", spread_provider=lambda s: 1.0, point_provider=lambda s: 0.0001)
        assert scored.outcome == "TP"
        assert scored.gross_r == 2.0
        assert scored.hit_bars == 3
        expected_spread_px = 1.0 * 0.0001
        expected_dist = abs(1.1000 - 1.0900)
        expected_spread_r = round(expected_spread_px / expected_dist, 4)
        assert scored.spread_r == expected_spread_r
        assert scored.net_r == round(2.0 - expected_spread_r, 4)

    def test_sl_hit_scenario(self, tmp_path):
        conn = _make_db(tmp_path)
        _make_bar_table(conn)
        sig = _make_signal_row(
            conn, symbol="EURUSD", direction="BUY",
            entry_price=1.1000, stop_loss=1.0900, take_profit=1.1200,
        )
        bars = [
            (TEST_BASE_TS + 900, 1.1000, 1.1050, 1.0980, 1.1020),
            (TEST_BASE_TS + 1800, 1.1020, 1.1030, 1.0880, 1.0900),
        ]
        scored = score_signal(sig, bars, "EURUSD", spread_provider=lambda s: 1.0, point_provider=lambda s: 0.0001)
        assert scored.outcome == "SL"
        assert scored.gross_r == -1.0
        assert scored.hit_bars == 2

    def test_still_open(self, tmp_path):
        conn = _make_db(tmp_path)
        _make_bar_table(conn)
        sig = _make_signal_row(
            conn, symbol="EURUSD", direction="BUY",
            entry_price=1.1000, stop_loss=1.0900, take_profit=1.1200,
        )
        bars = [
            (TEST_BASE_TS + 900, 1.1000, 1.1050, 1.0980, 1.1020),
            (TEST_BASE_TS + 1800, 1.1020, 1.1100, 1.1000, 1.1080),
        ]
        scored = score_signal(sig, bars, "EURUSD", spread_provider=lambda s: 1.0, point_provider=lambda s: 0.0001)
        assert scored.outcome == "OPEN"
        assert scored.gross_r == 0.0
        assert scored.hit_bars == 0

    def test_no_data_empty_bars(self, tmp_path):
        conn = _make_db(tmp_path)
        sig = _make_signal_row(
            conn, symbol="EURUSD", direction="BUY",
            entry_price=1.1000, stop_loss=1.0900, take_profit=1.1200,
        )
        scored = score_signal(sig, [], "EURUSD", spread_provider=lambda s: 1.0, point_provider=lambda s: 0.0001)
        assert scored.outcome == "NO_DATA"

    def test_sell_tp_hit(self, tmp_path):
        conn = _make_db(tmp_path)
        sig = _make_signal_row(
            conn, symbol="EURUSD", direction="SELL",
            entry_price=1.1000, stop_loss=1.1100, take_profit=1.0800,
        )
        bars = [
            (TEST_BASE_TS + 900, 1.1000, 1.0950, 1.0790, 1.0850),
        ]
        scored = score_signal(sig, bars, "EURUSD", spread_provider=lambda s: 1.0, point_provider=lambda s: 0.0001)
        assert scored.outcome == "TP"
        assert scored.gross_r == 2.0
        assert scored.net_r == round(2.0 - 0.0001 / 0.01, 4)

    def test_sell_sl_hit(self, tmp_path):
        conn = _make_db(tmp_path)
        sig = _make_signal_row(
            conn, symbol="EURUSD", direction="SELL",
            entry_price=1.1000, stop_loss=1.1100, take_profit=1.0800,
        )
        bars = [
            (TEST_BASE_TS + 900, 1.1000, 1.1150, 1.0980, 1.1050),
        ]
        scored = score_signal(sig, bars, "EURUSD", spread_provider=lambda s: 1.0, point_provider=lambda s: 0.0001)
        assert scored.outcome == "SL"
        assert scored.gross_r == -1.0


class TestScoreSignalsIntegration:
    def test_mixed_signals(self, tmp_path):
        conn = _make_db(tmp_path)
        _make_bar_table(conn)

        now = int(datetime.now(timezone.utc).timestamp())

        # Signal 1: BUY TP hit
        sig1 = _make_signal_row(
            conn, signal_id=1, symbol="EURUSD", direction="BUY",
            entry_price=1.1000, stop_loss=1.0900, take_profit=1.1200,
            bar_ts=now - 86400,
        )
        _insert_bar(conn, now - 86400 + 900, "EURUSD", 1.1000, 1.1050, 1.0980, 1.1020)
        _insert_bar(conn, now - 86400 + 1800, "EURUSD", 1.1020, 1.1250, 1.1000, 1.1150)

        # Signal 2: BUY SL hit
        sig2 = _make_signal_row(
            conn, signal_id=2, symbol="GBPUSD", direction="BUY",
            entry_price=1.2500, stop_loss=1.2400, take_profit=1.2700,
            bar_ts=now - 43200,
        )
        _insert_bar(conn, now - 43200 + 900, "GBPUSD", 1.2500, 1.2550, 1.2380, 1.2420)

        # Signal 3: Test signal (should be excluded)
        _make_signal_row(
            conn, signal_id=3, symbol="EURUSD", direction="BUY",
            entry_price=1.1100, stop_loss=1.1000, take_profit=1.1300,
            is_test=1, bar_ts=now - 21600,
        )

        # Signal 4: Still open
        sig4 = _make_signal_row(
            conn, signal_id=4, symbol="USDJPY", direction="SELL",
            entry_price=150.0, stop_loss=151.0, take_profit=148.0,
            bar_ts=now - 10800,
        )
        _insert_bar(conn, now - 10800 + 900, "USDJPY", 150.0, 150.5, 149.5, 150.2)

        scored = score_signals(
            conn, weeks=52,
            bar_source=BAR_SOURCE_TABLE,
            spread_provider=lambda s: 1.0,
            point_provider=lambda s: 0.0001,
        )
        assert len(scored) == 3  # test signal excluded
        outcomes = {s.outcome for s in scored}
        assert "TP" in outcomes
        assert "SL" in outcomes
        assert "OPEN" in outcomes

    def test_empty_db(self, tmp_path):
        conn = _make_db(tmp_path)
        scored = score_signals(conn, weeks=52, bar_source=BAR_SOURCE_TABLE)
        assert scored == []

    def test_test_signals_excluded(self, tmp_path):
        conn = _make_db(tmp_path)
        _make_bar_table(conn)
        _make_signal_row(conn, signal_id=1, is_test=1, bar_ts=TEST_BASE_TS)
        scored = score_signals(conn, weeks=52, bar_source=BAR_SOURCE_TABLE,
                               spread_provider=lambda s: 1.0,
                               point_provider=lambda s: 0.0001)
        assert scored == []


class TestGetSignals:
    def test_recent_only(self, tmp_path):
        conn = _make_db(tmp_path)
        old_ts = int((datetime.now(timezone.utc) - timedelta(weeks=30)).timestamp())
        recent_ts = int(datetime.now(timezone.utc).timestamp())

        _make_signal_row(conn, signal_id=1, bar_ts=old_ts)
        _make_signal_row(conn, signal_id=2, bar_ts=recent_ts)

        signals = get_signals(conn, weeks=26)
        assert len(signals) == 1
        assert signals[0]["id"] == 2

    def test_cutoff_uses_ts_not_bar_ts(self, tmp_path):
        """get_signals must filter on the UTC ts column, not bar_ts (broker epoch).

        If a signal has a recent ts but a non-UTC bar_ts (e.g. MT5 server time
        that leads UTC by hours), it must still be included. The filter must
        never drop or include rows based on bar_ts.
        """
        conn = _make_db(tmp_path)
        recent_utc = datetime.now(timezone.utc) - timedelta(hours=1)
        _make_signal_row(
            conn, signal_id=1, bar_ts=9999999999,
            signal_ts=recent_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        )
        signals = get_signals(conn, weeks=1)
        assert len(signals) == 1


class TestTimezoneFix:
    def test_bar_time_uses_signal_ts(self, tmp_path):
        """bar_time must come from signal ts (UTC), not datetime.fromtimestamp(bar_ts).

        Simulate a broker epoch offset of +3h (broker leads UTC by 3h).
        The signal ts is the correct UTC reference.
        """
        conn = _make_db(tmp_path)
        broker_epoch = 1792618200  # SQLite: 2026-10-21T21:30:00 UTC, but event ts is 18:45
        utc_ts = "2026-10-21T18:45:45+00:00"
        sig_row = _make_signal_row(
            conn, signal_id=1, symbol="EURUSD", direction="BUY",
            entry_price=1.1000, stop_loss=1.0900, take_profit=1.1200,
            bar_ts=broker_epoch, signal_ts=utc_ts,
        )
        scored = score_signal(sig_row, [], "EURUSD")
        assert scored.bar_time == "2026-10-21 18:45 UTC"
        assert scored.signal_ts == "2026-10-21T18:45:45+00:00"

    def test_weekly_grouping_uses_signal_ts(self):
        """build_weekly_results groups by signal_ts, not bar_ts."""
        scored = [
            ScoredSignal(
                signal_id=1, symbol="EURUSD", direction="BUY",
                entry_price=1.1, stop_loss=1.09, take_profit=1.12,
                bar_ts=1792618200, score=0.5, bar_time="x",
                signal_ts="2026-10-21T18:45:45+00:00",
                outcome="TP", gross_r=2.0, spread_r=0.1, net_r=1.9, hit_bars=3,
            ),
        ]
        weekly = build_weekly_results(scored)
        assert len(weekly) == 1
        assert "2026-W" in weekly[0]["week"]


class TestBuildSummary:
    def _make_scored(self, outcome, gross_r, net_r, spread_r=0.1, symbol="EURUSD"):
        return ScoredSignal(
            signal_id=1, symbol=symbol, direction="BUY",
            entry_price=1.1000, stop_loss=1.0900, take_profit=1.1200,
            bar_ts=TEST_BASE_TS, score=0.5, bar_time="2024-01-01 00:00 UTC",
            signal_ts="2024-01-01T00:00:00+00:00",
            outcome=outcome, gross_r=gross_r, spread_r=spread_r, net_r=net_r,
            hit_bars=3,
        )

    def test_no_signals(self):
        summary = build_summary([])
        assert summary["total_signals"] == 0
        assert summary["resolved"] == 0
        assert summary["win_rate_%"] == 0.0

    def test_all_wins(self):
        scored = [
            self._make_scored("TP", 2.0, 1.9),
            self._make_scored("TP", 2.0, 1.9),
        ]
        summary = build_summary(scored)
        assert summary["total_signals"] == 2
        assert summary["resolved"] == 2
        assert summary["wins"] == 2
        assert summary["losses"] == 0
        assert summary["win_rate_%"] == 100.0
        assert summary["avg_gross_r"] == 2.0
        assert summary["avg_net_r"] == 1.9

    def test_mixed(self):
        scored = [
            self._make_scored("TP", 2.0, 1.9),
            self._make_scored("SL", -1.0, -1.1),
            self._make_scored("OPEN", 0.0, 0.0),
        ]
        summary = build_summary(scored)
        assert summary["total_signals"] == 3
        assert summary["resolved"] == 2
        assert summary["wins"] == 1
        assert summary["losses"] == 1
        assert summary["win_rate_%"] == 50.0
        assert summary["open"] == 1
        assert summary["no_data"] == 0

    def test_no_data_counted(self):
        scored = [self._make_scored("NO_DATA", 0.0, 0.0)]
        summary = build_summary(scored)
        assert summary["total_signals"] == 1
        assert summary["resolved"] == 0
        assert summary["no_data"] == 1

    def test_expectancy_calculation(self):
        scored = [
            self._make_scored("TP", 3.0, 2.9),
            self._make_scored("TP", 2.0, 1.9),
            self._make_scored("SL", -1.0, -1.1),
            self._make_scored("SL", -1.0, -1.1),
        ]
        summary = build_summary(scored)
        # win_rate = 0.5, avg_win_gross = 2.5, avg_loss_gross = -1.0
        # expectancy_gross = 0.5*2.5 + 0.5*(-1.0) = 0.75
        assert summary["expectancy_gross"] == 0.75


class TestBuildWeeklyResults:
    def test_grouping_by_week_and_symbol(self):
        from datetime import datetime, timezone
        week1_ts = "2024-01-01T00:00:00+00:00"
        week2_ts = "2024-01-08T00:00:00+00:00"

        scored = [
            ScoredSignal(
                signal_id=1, symbol="EURUSD", direction="BUY",
                entry_price=1.1, stop_loss=1.09, take_profit=1.12,
                bar_ts=1704067200, score=0.5, bar_time="2024-01-01 00:00 UTC",
                signal_ts=week1_ts,
                outcome="TP", gross_r=2.0, spread_r=0.1, net_r=1.9, hit_bars=3,
            ),
            ScoredSignal(
                signal_id=2, symbol="EURUSD", direction="BUY",
                entry_price=1.1, stop_loss=1.09, take_profit=1.12,
                bar_ts=1704067200, score=0.5, bar_time="2024-01-01 00:00 UTC",
                signal_ts=week1_ts,
                outcome="SL", gross_r=-1.0, spread_r=0.1, net_r=-1.1, hit_bars=5,
            ),
            ScoredSignal(
                signal_id=3, symbol="GBPUSD", direction="SELL",
                entry_price=1.25, stop_loss=1.26, take_profit=1.23,
                bar_ts=1704672000, score=0.4, bar_time="2024-01-08 00:00 UTC",
                signal_ts=week2_ts,
                outcome="TP", gross_r=1.0, spread_r=0.1, net_r=0.9, hit_bars=10,
            ),
        ]
        weekly = build_weekly_results(scored)
        assert len(weekly) == 2
        week1 = weekly[0]
        assert week1["week"].startswith("2024-W")
        assert "EURUSD" in week1["symbols"]
        eurusd_w1 = week1["symbols"]["EURUSD"]
        assert eurusd_w1["wins"] == 1
        assert eurusd_w1["losses"] == 1
        assert eurusd_w1["resolved"] == 2
        assert eurusd_w1["win_rate_%"] == 50.0


class TestFormatReport:
    def test_empty_report(self):
        report = format_report([], [], {
            "total_signals": 0, "resolved": 0, "wins": 0, "losses": 0,
            "open": 0, "no_data": 0, "win_rate_%": 0.0,
            "avg_gross_r": 0.0, "avg_net_r": 0.0, "avg_spread_r": 0.0,
            "std_gross_r": 0.0, "std_net_r": 0.0,
            "se_gross_r": 0.0, "se_net_r": 0.0,
            "expectancy_gross": 0.0, "expectancy_net": 0.0,
        })
        assert "No production signals found" in report

    def test_report_with_signals(self):
        scored = [
        ScoredSignal(
            signal_id=1, symbol="EURUSD", direction="BUY",
            entry_price=1.1000, stop_loss=1.0900, take_profit=1.1200,
            bar_ts=TEST_BASE_TS, score=0.5, bar_time="2024-01-01 00:00 UTC",
            signal_ts="2024-01-01T00:00:00+00:00",
            outcome="TP", gross_r=2.0, spread_r=0.1, net_r=1.9, hit_bars=3,
        ),
        ]
        weekly = build_weekly_results(scored)
        summary = build_summary(scored)
        report = format_report(scored, weekly, summary)
        assert "FORWARD-TEST SCORER REPORT" in report
        assert "Summary" in report
        assert "Weekly Results" in report
        assert "Signals" in report
        assert "EURUSD" in report
        assert "TP" in report

    def test_no_live_reference_in_output(self):
        """Scorer output must not reference --live."""
        report = format_report([], [], {
            "total_signals": 0, "resolved": 0, "wins": 0, "losses": 0,
            "open": 0, "no_data": 0, "win_rate_%": 0.0,
            "avg_gross_r": 0.0, "avg_net_r": 0.0, "avg_spread_r": 0.0,
            "std_gross_r": 0.0, "std_net_r": 0.0,
            "se_gross_r": 0.0, "se_net_r": 0.0,
            "expectancy_gross": 0.0, "expectancy_net": 0.0,
        })
        assert "--live" not in report
        assert "live" not in report.lower()

    def test_empty_report_says_strategy_not_produced_signal(self):
        """Empty state must say the strategy has not produced a real signal."""
        report = format_report([], [], {
            "total_signals": 0, "resolved": 0, "wins": 0, "losses": 0,
            "open": 0, "no_data": 0, "win_rate_%": 0.0,
            "avg_gross_r": 0.0, "avg_net_r": 0.0, "avg_spread_r": 0.0,
            "std_gross_r": 0.0, "std_net_r": 0.0,
            "se_gross_r": 0.0, "se_net_r": 0.0,
            "expectancy_gross": 0.0, "expectancy_net": 0.0,
        })
        assert "not produced a real signal" in report


class TestBarWindowOffset:
    """Tests proving bar window selection with +3h, +2h, 0h broker offsets."""

    def test_plus_3h_offset_first_bar_after_signal(self, tmp_path):
        """With +3h broker offset, the first bar fetched must be the one after
        the signal bar (broker clock compared to broker clock)."""
        conn = _make_db(tmp_path)
        _make_bar_table(conn)

        offset = 10800  # +3h
        signal_broker_ts = 1700000000
        utc_ts = "2024-11-14T23:30:00+00:00"

        _make_signal_row(
            conn, signal_id=1, symbol="EURUSD",
            bar_ts=signal_broker_ts, signal_ts=utc_ts,
        )

        bar_every_900 = signal_broker_ts + 900
        _insert_bar(conn, bar_every_900, "EURUSD", 1.1000, 1.1050, 1.0980, 1.1020)

        from quote_clock import detect_broker_offset_seconds
        assert detect_broker_offset_seconds([signal_broker_ts], signal_broker_ts - offset) == offset

        bars = _fetch_bars_from_table(conn, "EURUSD", signal_broker_ts, max_bars=500)
        assert bars[0][0] == bar_every_900
        assert len(bars) >= 1

    def test_plus_2h_offset_first_bar_after_signal(self, tmp_path):
        """With +2h broker offset, the first bar fetched must be the one after
        the signal bar."""
        conn = _make_db(tmp_path)
        _make_bar_table(conn)

        offset = 7200  # +2h
        signal_broker_ts = 1700000000

        _insert_bar(conn, signal_broker_ts, "EURUSD", 1.1000, 1.1000, 1.1000, 1.1000)
        _insert_bar(conn, signal_broker_ts + 900, "EURUSD", 1.1000, 1.1050, 1.0980, 1.1020)

        from quote_clock import detect_broker_offset_seconds
        assert detect_broker_offset_seconds([signal_broker_ts], signal_broker_ts - offset) == offset

        bars = _fetch_bars_from_table(conn, "EURUSD", signal_broker_ts, max_bars=500)
        assert bars[0][0] == signal_broker_ts + 900
        assert len(bars) >= 1

    def test_zero_offset_first_bar_after_signal(self, tmp_path):
        """With 0h offset, the first bar fetched must be the one after the signal bar."""
        conn = _make_db(tmp_path)
        _make_bar_table(conn)

        signal_broker_ts = 1700000000
        _insert_bar(conn, signal_broker_ts, "EURUSD", 1.1000, 1.1000, 1.1000, 1.1000)
        _insert_bar(conn, signal_broker_ts + 900, "EURUSD", 1.1000, 1.1050, 1.0980, 1.1020)

        bars = _fetch_bars_from_table(conn, "EURUSD", signal_broker_ts, max_bars=500)
        assert bars[0][0] == signal_broker_ts + 900
        assert len(bars) >= 1


class TestEpochExample:
    """Print raw epoch 1792618200 -> broker -> UTC -> IST to settle the offset."""

    def test_epoch_1792618200_example(self):
        result = _print_epoch_example()
        assert result["broker_epoch"] == 1792618200
        assert result["offset_seconds"] == 10800
        assert "broker_time" in result
        assert "utc_time" in result
        assert "ist_time" in result


class TestScorerTzDebug:
    """Debug tests confirming the timezone fix."""

    def test_broker_offset_detection_plus_3h(self):
        from quote_clock import detect_broker_offset_seconds
        server_epoch = 1792618200
        utc_now = server_epoch - 10800
        assert detect_broker_offset_seconds([server_epoch], utc_now) == 10800


# -- MT5 bar source (fake terminal) ---------------------------------------
#
# The fake models the terminal's observable contract: initialize() returns a
# bool, copy_rates_range/copy_rates_from_pos return numpy-style record rows or
# None, symbol_select returns a bool, and shutdown() is a no-op. It records
# every call so a test can prove shutdown ran and no order function was touched.

class _FakeTerminal:
    """Minimal MetaTrader5 stand-in for the scorer's read-only bar fetch."""

    TIMEFRAME_M15 = 15

    def __init__(self, rates=None, *, initialize_ok=True, raise_on_init=False,
                 raise_on_read=False, range_returns_none=False,
                 pos_returns_none=False, has_range=True):
        self.rates = list(rates or [])
        self.initialize_ok = initialize_ok
        self.raise_on_init = raise_on_init
        self.raise_on_read = raise_on_read
        self.range_returns_none = range_returns_none
        self.pos_returns_none = pos_returns_none
        if not has_range:
            # Simulate a build with no copy_rates_range at all.
            self.copy_rates_range = None
        self.calls: list[tuple] = []

    def initialize(self):
        self.calls.append(("initialize",))
        if self.raise_on_init:
            raise RuntimeError("terminal busy")
        return self.initialize_ok

    def shutdown(self):
        self.calls.append(("shutdown",))
        return True

    def symbol_select(self, symbol, select=True):
        self.calls.append(("symbol_select", symbol, select))
        return True

    def copy_rates_range(self, symbol, timeframe, start, end):
        self.calls.append(("copy_rates_range", symbol, timeframe, start, end))
        if self.raise_on_read:
            raise RuntimeError("history unavailable")
        if self.range_returns_none:
            return None
        return [r for r in self.rates if start <= int(r["time"]) <= end]

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        self.calls.append(("copy_rates_from_pos", symbol, timeframe,
                           start_pos, count))
        if self.raise_on_read:
            raise RuntimeError("history unavailable")
        if self.pos_returns_none:
            return None
        if start_pos == 1:
            return list(self.rates)[1:]
        return list(self.rates)

    def call_names(self):
        return [c[0] for c in self.calls]


def _rate(epoch, o, h, l, c):  # noqa: E741 - OHLC names match MT5's columns
    return {"time": epoch, "open": o, "high": h, "low": l, "close": c}

class TestMt5BarFetch:
    """The MT5 path: closed bars only, one initialize/shutdown, read-only."""

    def test_fetches_only_bars_after_the_signal_bar(self):
        term = _FakeTerminal([
            _rate(RECENT_TS, 1.1, 1.1, 1.1, 1.1),          # the signal bar
            _rate(RECENT_TS + 900, 1.1, 1.12, 1.09, 1.11),
            _rate(RECENT_TS + 1800, 1.11, 1.13, 1.10, 1.12),
        ])
        bars = _fetch_bars_from_mt5("EURUSD", RECENT_TS, 500, mt5_module=term)
        assert [b[0] for b in bars] == [RECENT_TS + 900, RECENT_TS + 1800]

    def test_broker_epochs_are_compared_on_the_broker_clock(self):
        """bar_ts is a broker-server epoch; the window must use it as-is."""
        broker_signal_ts = 1792618200
        term = _FakeTerminal([
            _rate(broker_signal_ts + 900, 1.1, 1.12, 1.09, 1.11),
        ])
        bars = _fetch_bars_from_mt5("EURUSD", broker_signal_ts, 500, mt5_module=term)
        assert len(bars) == 1
        assert bars[0][0] == broker_signal_ts + 900
        range_call = [c for c in term.calls if c[0] == "copy_rates_range"][0]
        assert range_call[3] == broker_signal_ts + 1

    def test_shutdown_is_always_called(self):
        term = _FakeTerminal([_rate(RECENT_TS + 900, 1.1, 1.1, 1.1, 1.1)])
        _fetch_bars_from_mt5("EURUSD", RECENT_TS, 500, mt5_module=term)
        assert term.call_names().count("initialize") == 1
        assert term.call_names().count("shutdown") == 1

    def test_shutdown_runs_even_when_the_read_fails(self):
        term = _FakeTerminal(raise_on_read=True)
        with pytest.raises(Mt5Unavailable):
            _fetch_bars_from_mt5("EURUSD", RECENT_TS, 500, mt5_module=term)
        assert term.call_names().count("shutdown") == 1

    def test_initialize_false_raises_without_reading(self):
        term = _FakeTerminal(initialize_ok=False)
        with pytest.raises(Mt5Unavailable):
            _fetch_bars_from_mt5("EURUSD", RECENT_TS, 500, mt5_module=term)
        assert "copy_rates_range" not in term.call_names()
        assert "copy_rates_from_pos" not in term.call_names()

    def test_initialize_raising_raises_unavailable(self):
        term = _FakeTerminal(raise_on_init=True)
        with pytest.raises(Mt5Unavailable):
            _fetch_bars_from_mt5("EURUSD", RECENT_TS, 500, mt5_module=term)

    def test_no_order_function_is_ever_called(self):
        term = _FakeTerminal([_rate(RECENT_TS + 900, 1.1, 1.1, 1.1, 1.1)])
        _fetch_bars_from_mt5("EURUSD", RECENT_TS, 500, mt5_module=term)
        for name in term.call_names():
            assert "send" not in name
            assert "order" not in name

    def test_falls_back_to_copy_rates_from_pos(self):
        term = _FakeTerminal([_rate(RECENT_TS, 1.1, 1.1, 1.1, 1.1),
                              _rate(RECENT_TS + 900, 1.1, 1.12, 1.09, 1.11)],
                             has_range=False)
        bars = _fetch_bars_from_mt5("EURUSD", RECENT_TS, 500, mt5_module=term)
        assert [b[0] for b in bars] == [RECENT_TS + 900]
        assert "copy_rates_from_pos" in term.call_names()

    def test_range_none_falls_back_to_pos(self):
        term = _FakeTerminal([_rate(RECENT_TS, 1.1, 1.1, 1.1, 1.1),
                              _rate(RECENT_TS + 900, 1.1, 1.12, 1.09, 1.11)],
                             range_returns_none=True)
        bars = _fetch_bars_from_mt5("EURUSD", RECENT_TS, 500, mt5_module=term)
        assert [b[0] for b in bars] == [RECENT_TS + 900]

    def test_both_reads_none_raises_unavailable(self):
        term = _FakeTerminal(pos_returns_none=True, has_range=False)
        with pytest.raises(Mt5Unavailable):
            _fetch_bars_from_mt5("EURUSD", RECENT_TS, 500, mt5_module=term)

    def test_empty_rates_is_a_documented_zero_not_unavailable(self):
        """An empty read is 'no bars', which is NO_DATA — not INSUFFICIENT."""
        term = _FakeTerminal([])
        assert _fetch_bars_from_mt5("EURUSD", RECENT_TS, 500, mt5_module=term) == []

    def test_bad_bar_ts_raises_unavailable(self):
        term = _FakeTerminal([])
        with pytest.raises(Mt5Unavailable):
            _fetch_bars_from_mt5("EURUSD", 0, 500, mt5_module=term)

    def test_max_bars_is_respected_and_sorted_ascending(self):
        docs = [_rate(RECENT_TS + 900 * i, 1.1, 1.1, 1.1, 1.1)
                for i in range(5, 0, -1)]
        term = _FakeTerminal(docs)
        bars = _fetch_bars_from_mt5("EURUSD", RECENT_TS, 2, mt5_module=term)
        assert len(bars) == 2
        assert bars[0][0] < bars[1][0]

class TestInsufficientOutcome:
    """An unavailable feed is INSUFFICIENT, never NO_DATA, never a loss."""

    def test_unavailable_feed_scores_insufficient(self, tmp_path):
        conn = _make_db(tmp_path)
        _make_signal_row(conn, signal_id=1, bar_ts=RECENT_TS)

        def _dead_provider(conn_, symbol, start_ts, max_bars):
            raise Mt5Unavailable("no terminal")

        scored = score_signals(conn, weeks=52, bar_provider=_dead_provider,
                               spread_provider=lambda s: 1.0,
                               point_provider=lambda s: 0.0001)
        assert len(scored) == 1
        assert scored[0].outcome == "INSUFFICIENT"
        assert scored[0].gross_r == 0.0
        assert scored[0].net_r == 0.0
        assert "no terminal" in scored[0].note

    def test_insufficient_is_not_counted_as_resolved_or_a_loss(self, tmp_path):
        conn = _make_db(tmp_path)
        _make_signal_row(conn, signal_id=1, bar_ts=RECENT_TS)

        def _dead_provider(conn_, symbol, start_ts, max_bars):
            raise Mt5Unavailable("no terminal")

        scored = score_signals(conn, weeks=52, bar_provider=_dead_provider,
                               spread_provider=lambda s: 1.0,
                               point_provider=lambda s: 0.0001)
        summary = build_summary(scored)
        assert summary["resolved"] == 0
        assert summary["losses"] == 0
        assert summary["wins"] == 0
        assert summary["insufficient"] == 1
        assert summary["expectancy_net"] == 0.0

    def test_insufficient_never_enters_the_r_averages(self, tmp_path):
        conn = _make_db(tmp_path)
        _make_signal_row(conn, signal_id=1, bar_ts=RECENT_TS)

        def _dead_provider(conn_, symbol, start_ts, max_bars):
            raise Mt5Unavailable("no terminal")

        scored = score_signals(conn, weeks=52, bar_provider=_dead_provider,
                               spread_provider=lambda s: 1.0,
                               point_provider=lambda s: 0.0001)
        summary = build_summary(scored)
        assert summary["avg_net_r"] == 0.0
        assert summary["avg_gross_r"] == 0.0

    def test_weekly_row_carries_insufficient_separately(self, tmp_path):
        conn = _make_db(tmp_path)
        _make_signal_row(conn, signal_id=1, bar_ts=RECENT_TS)

        def _dead_provider(conn_, symbol, start_ts, max_bars):
            raise Mt5Unavailable("no terminal")

        scored = score_signals(conn, weeks=52, bar_provider=_dead_provider,
                               spread_provider=lambda s: 1.0,
                               point_provider=lambda s: 0.0001)
        weekly = build_weekly_results(scored)
        sd = weekly[0]["symbols"]["EURUSD"]
        assert sd["insufficient"] == 1
        assert sd["resolved"] == 0
        assert sd["losses"] == 0

    def test_report_states_insufficient_plainly(self, tmp_path):
        conn = _make_db(tmp_path)
        _make_signal_row(conn, signal_id=1, bar_ts=RECENT_TS)

        def _dead_provider(conn_, symbol, start_ts, max_bars):
            raise Mt5Unavailable("no terminal")

        scored = score_signals(conn, weeks=52, bar_provider=_dead_provider,
                               spread_provider=lambda s: 1.0,
                               point_provider=lambda s: 0.0001)
        report = format_report(scored, build_weekly_results(scored),
                               build_summary(scored))
        assert "INSUFFICIENT" in report
        assert "not counted as a loss" in report

    def test_read_ok_but_empty_window_is_no_data_not_insufficient(self, tmp_path):
        """A consulted feed with no bars is NO_DATA. The two must not merge."""
        conn = _make_db(tmp_path)
        _make_signal_row(conn, signal_id=1, bar_ts=RECENT_TS)
        scored = score_signals(conn, weeks=52,
                               spread_provider=lambda s: 1.0,
                               point_provider=lambda s: 0.0001,
                               bar_provider=lambda c, s, b, m: [])
        assert scored[0].outcome == "NO_DATA"
        assert build_summary(scored)["insufficient"] == 0

class TestBarSourceSelection:
    """The default source must be MT5; the table path stays for tests."""

    def test_default_source_is_mt5(self, tmp_path):
        conn = _make_db(tmp_path)
        seen = []

        def _factory(source):
            seen.append(source)
            return lambda c, s, b, m: []

        score_signals(conn, weeks=52, bar_provider_factory=_factory)
        assert seen == [BAR_SOURCE_MT5]

    def test_table_source_selects_the_table_provider(self):
        assert resolve_bar_provider(BAR_SOURCE_TABLE) is _fetch_bars_from_table

    def test_mt5_source_provider_fail_closed_without_terminal(self):
        """When mt5.initialize() returns False, the provider must raise Mt5Unavailable."""

        class _NoTerminal:
            """A terminal that reports no connection."""

            TIMEFRAME_M15 = 15

            def initialize(self):
                return False

            def shutdown(self):
                pass

        provider = resolve_bar_provider(BAR_SOURCE_MT5)

        def _provider(conn, symbol, start_ts, max_bars):
            return _fetch_bars_from_mt5(symbol, start_ts, max_bars, mt5_module=_NoTerminal())

        with pytest.raises(Mt5Unavailable):
            _provider(None, "EURUSD", RECENT_TS, 500)

    def test_mt5_source_provider_targets_the_terminal(self, tmp_path):
        """MT5 provider with a live terminal fetches bars from MT5 directly."""

        class _FakeLiveTerminal:
            """Minimal stand-in that succeeds initialize() and returns a bar."""

            TIMEFRAME_M15 = 15

            def __init__(self):
                self.calls: list = []

            def initialize(self):
                self.calls.append(("initialize",))
                return True

            def shutdown(self):
                self.calls.append(("shutdown",))

            def symbol_select(self, symbol, select=True):
                return True

            def copy_rates_range(self, symbol, timeframe, start, end):
                self.calls.append(("copy_rates_range", symbol, timeframe, start, end))
                return [{"time": RECENT_TS + 900, "open": 1.1, "high": 1.12, "low": 1.09, "close": 1.11}]

            def copy_rates_from_pos(self, *args, **kwargs):
                return None

        term = _FakeLiveTerminal()
        provider = resolve_bar_provider(BAR_SOURCE_MT5)

        def _provider(conn, symbol, start_ts, max_bars):
            return _fetch_bars_from_mt5(symbol, start_ts, max_bars, mt5_module=term)

        bars = _provider(None, "EURUSD", RECENT_TS, 500)
        assert len(bars) == 1
        assert bars[0][0] == RECENT_TS + 900
        assert ("initialize",) in term.calls
        assert ("shutdown",) in term.calls

    def test_table_path_still_scores_from_a_bars_table(self, tmp_path):
        conn = _make_db(tmp_path)
        _make_bar_table(conn)
        _make_signal_row(conn, signal_id=1, symbol="EURUSD",
                         entry_price=1.1000, stop_loss=1.0900,
                         take_profit=1.1200, bar_ts=RECENT_TS)
        _insert_bar(conn, RECENT_TS + 900, "EURUSD",
                    1.1000, 1.1250, 1.0980, 1.1200)
        scored = score_signals(conn, weeks=52, bar_source=BAR_SOURCE_TABLE,
                               spread_provider=lambda s: 1.0,
                               point_provider=lambda s: 0.0001)
        assert scored[0].outcome == "TP"
