"""Tests for the forward-test scorer."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta

import pytest

from scripts.forward_test_scorer import (
    ScoredSignal,
    asset_class_spread_points,
    build_summary,
    build_weekly_results,
    default_point_size,
    format_report,
    get_signals,
    score_bar_hit,
    score_signal,
    score_signals,
)


TEST_BASE_TS = 1700000000


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
):
    """Insert a signal row and return it as a Row."""
    if bar_ts is None:
        bar_ts = TEST_BASE_TS
    conn.execute(
            """INSERT INTO signals (symbol, direction, score, bar_ts, ts, decision_id,
                status, strategy, timeframe, entry_price, stop_loss, take_profit,
                volume, expires_at, decided_by, decided_at, blocks_json, reject_reason,
                superseded_by_decision_id, is_test, test_tag)
               VALUES (?, ?, ?, ?, ?, ?, 'EXECUTED', 'confluence', 'M15', ?, ?, ?,
                0.01, ?, 'engine', NULL, '{}', NULL, NULL, ?, NULL)""",
            (symbol, direction, float(score), bar_ts,
             datetime.fromtimestamp(bar_ts, tz=timezone.utc).isoformat(),
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
        scored = score_signals(conn, weeks=52)
        assert scored == []

    def test_test_signals_excluded(self, tmp_path):
        conn = _make_db(tmp_path)
        _make_bar_table(conn)
        _make_signal_row(conn, signal_id=1, is_test=1, bar_ts=TEST_BASE_TS)
        scored = score_signals(conn, weeks=52, spread_provider=lambda s: 1.0, point_provider=lambda s: 0.0001)
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


class TestBuildSummary:
    def _make_scored(self, outcome, gross_r, net_r, spread_r=0.1, symbol="EURUSD"):
        return ScoredSignal(
            signal_id=1, symbol=symbol, direction="BUY",
            entry_price=1.1000, stop_loss=1.0900, take_profit=1.1200,
            bar_ts=TEST_BASE_TS, score=0.5, bar_time="2024-01-01 00:00 UTC",
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
        week1_ts = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
        week2_ts = int(datetime(2024, 1, 8, tzinfo=timezone.utc).timestamp())

        scored = [
            ScoredSignal(
                signal_id=1, symbol="EURUSD", direction="BUY",
                entry_price=1.1, stop_loss=1.09, take_profit=1.12,
                bar_ts=week1_ts, score=0.5, bar_time="x",
                outcome="TP", gross_r=2.0, spread_r=0.1, net_r=1.9, hit_bars=3,
            ),
            ScoredSignal(
                signal_id=2, symbol="EURUSD", direction="BUY",
                entry_price=1.1, stop_loss=1.09, take_profit=1.12,
                bar_ts=week1_ts, score=0.5, bar_time="x",
                outcome="SL", gross_r=-1.0, spread_r=0.1, net_r=-1.1, hit_bars=5,
            ),
            ScoredSignal(
                signal_id=3, symbol="GBPUSD", direction="SELL",
                entry_price=1.25, stop_loss=1.26, take_profit=1.23,
                bar_ts=week2_ts, score=0.4, bar_time="x",
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
