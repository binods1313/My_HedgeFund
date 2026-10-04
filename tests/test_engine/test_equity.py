r"""Tests for equity baselines and the trading-day boundary.

The day boundary is the subtle part. Measured on live demo accounts,
symbol_info_tick().time was up to 24 hours stale on some symbols and correct
to the minute on others, so a single unvalidated sample would seed the wrong
trading day -- and the daily loss limit resets on that day boundary. A limit
that resets at the wrong time is not a limit.

Run: python -m pytest tests/test_engine/test_equity.py -v
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from engine.equity import (
    MAX_CLOCK_SKEW_SECONDS,
    EquityBaselines,
    EquityTracker,
    broker_today,
    validate_server_epoch,
)
from engine.journal import Journal

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "equity.db")
    yield j
    j.close()


@pytest.fixture
def tracker(journal):
    return EquityTracker(journal)


# ─────────────────────────────────────────────────────────────────────────────
# Server-clock validation
# ─────────────────────────────────────────────────────────────────────────────

class TestServerEpochValidation:

    def test_a_plausible_epoch_is_accepted(self):
        assert validate_server_epoch(int(NOW), local_epoch=NOW) == int(NOW)

    def test_a_one_day_stale_epoch_is_rejected(self):
        """The value measured on the XM account for EURUSD."""
        stale = int(NOW) - 24 * 3600
        assert validate_server_epoch(stale, local_epoch=NOW) is None, (
            "a tick time a day out would seed the wrong trading day"
        )

    def test_a_four_hour_offset_is_rejected(self):
        """The offset measured for BTCUSD# was -3.00 h."""
        skewed = int(NOW) - 3 * 3600
        assert validate_server_epoch(skewed, local_epoch=NOW) is None

    def test_a_minor_offset_is_accepted(self):
        near = int(NOW) - 120        # two minutes
        assert validate_server_epoch(near, local_epoch=NOW) == near

    def test_exactly_at_the_tolerance_is_accepted(self):
        edge = int(NOW) - MAX_CLOCK_SKEW_SECONDS
        assert validate_server_epoch(edge, local_epoch=NOW) == edge

    def test_just_past_the_tolerance_is_rejected(self):
        past = int(NOW) - MAX_CLOCK_SKEW_SECONDS - 60
        assert validate_server_epoch(past, local_epoch=NOW) is None

    def test_missing_epoch_is_rejected(self):
        assert validate_server_epoch(None, local_epoch=NOW) is None
        assert validate_server_epoch(0, local_epoch=NOW) is None

    def test_a_future_epoch_is_rejected(self):
        ahead = int(NOW) + 24 * 3600
        assert validate_server_epoch(ahead, local_epoch=NOW) is None


class TestBrokerDay:

    def test_uses_the_broker_epoch_when_given(self):
        epoch = int(datetime(2026, 10, 2, 23, 30, tzinfo=timezone.utc).timestamp())
        assert broker_today(epoch) == "2026-10-02"

    def test_falls_back_to_utc_without_an_epoch(self):
        expected = datetime.now(timezone.utc).date().isoformat()
        assert broker_today(None) == expected

    def test_unvalidated_epoch_still_names_a_day(self):
        """broker_today does not itself validate.

        It is a pure formatter. Validation is validate_server_epoch's job, and
        the engine calls both. A test pins that the two are not confused.
        """
        stale = int(datetime(2026, 10, 2, 23, 30, tzinfo=timezone.utc).timestamp())
        # broker_today would return 2026-10-02 ...
        assert broker_today(stale) == "2026-10-02"
        # ... but validation refuses to hand it over for use.
        assert validate_server_epoch(stale, local_epoch=NOW) is None


# ─────────────────────────────────────────────────────────────────────────────
# Baselines
# ─────────────────────────────────────────────────────────────────────────────

class TestBaselines:

    def test_first_sample_seeds_both_baselines(self, tracker):
        baselines = tracker.observe(100_000.0, now_day="2026-10-04")
        assert baselines.start_of_day_equity == 100_000.0
        assert baselines.peak_equity == 100_000.0
        assert baselines.sample_count == 1

    def test_peak_rises_with_a_higher_reading(self, tracker):
        tracker.observe(100_000.0, now_day="2026-10-04")
        baselines = tracker.observe(105_000.0, now_day="2026-10-04")
        assert baselines.peak_equity == 105_000.0

    def test_peak_does_not_fall_on_a_lower_reading(self, tracker):
        """A drawdown must not lower the high-water mark."""
        tracker.observe(100_000.0, now_day="2026-10-04")
        baselines = tracker.observe(90_000.0, now_day="2026-10-04")
        assert baselines.peak_equity == 100_000.0, (
            "a lower reading must never become the peak"
        )

    def test_start_of_day_never_moves_within_a_day(self, tracker):
        tracker.observe(100_000.0, now_day="2026-10-04")
        baselines = tracker.observe(120_000.0, now_day="2026-10-04")
        assert baselines.start_of_day_equity == 100_000.0, (
            "the daily baseline is fixed at the first reading of the day"
        )

    def test_a_new_day_seeds_fresh_baselines(self, tracker):
        tracker.observe(100_000.0, now_day="2026-10-04")
        baselines = tracker.observe(95_000.0, now_day="2026-10-05")
        assert baselines.trading_day == "2026-10-05"
        assert baselines.start_of_day_equity == 95_000.0
        assert baselines.peak_equity == 95_000.0, (
            "a peak carried across a day boundary would make drawdown "
            "meaningless after a reset"
        )

    def test_non_positive_equity_is_refused(self, tracker):
        for bad in (0.0, -1.0, -99_999.0):
            with pytest.raises(ValueError):
                tracker.observe(bad, now_day="2026-10-04")

    def test_non_numeric_equity_is_refused(self, tracker):
        with pytest.raises(ValueError):
            tracker.observe("100000", now_day="2026-10-04")

    def test_current_is_none_before_any_sample(self, tracker):
        assert tracker.current("2026-10-04") is None

    def test_sample_count_increments(self, tracker):
        tracker.observe(100_000.0, now_day="2026-10-04")
        tracker.observe(100_500.0, now_day="2026-10-04")
        assert tracker.current("2026-10-04").sample_count == 2

    def test_each_day_is_stored_separately(self, tracker):
        tracker.observe(100_000.0, now_day="2026-10-04")
        tracker.observe(95_000.0, now_day="2026-10-05")
        day4 = tracker.current("2026-10-04")
        day5 = tracker.current("2026-10-05")
        assert day4.peak_equity == 100_000.0
        assert day5.peak_equity == 95_000.0

    def test_reset_day_drops_the_baseline(self, tracker):
        tracker.observe(100_000.0, now_day="2026-10-04")
        tracker.reset_day("2026-10-04")
        assert tracker.current("2026-10-04") is None


class TestDerivedPercentages:

    def _baselines(self, sod, peak, equity):
        return EquityBaselines(
            start_of_day_equity=sod, peak_equity=peak,
            trading_day="2026-10-04", sampled_at="", sample_count=1)

    def test_drawdown_from_peak(self):
        b = self._baselines(100_000.0, 110_000.0, 99_000.0)
        assert b.drawdown_pct(99_000.0) == pytest.approx(10.0)

    def test_drawdown_is_zero_at_the_peak(self):
        b = self._baselines(100_000.0, 110_000.0, 110_000.0)
        assert b.drawdown_pct(110_000.0) == pytest.approx(0.0)

    def test_drawdown_never_negative(self):
        b = self._baselines(100_000.0, 100_000.0, 120_000.0)
        assert b.drawdown_pct(120_000.0) == 0.0

    def test_drawdown_is_none_without_a_usable_peak(self):
        b = self._baselines(100_000.0, 0.0, 100_000.0)
        assert b.drawdown_pct(100_000.0) is None

    def test_daily_loss_from_start_of_day(self):
        b = self._baselines(100_000.0, 110_000.0, 97_000.0)
        assert b.daily_loss_pct(97_000.0) == pytest.approx(3.0)

    def test_daily_loss_never_negative(self):
        b = self._baselines(100_000.0, 100_000.0, 105_000.0)
        assert b.daily_loss_pct(105_000.0) == 0.0

    def test_daily_loss_is_none_without_a_usable_baseline(self):
        b = self._baselines(0.0, 100_000.0, 100_000.0)
        assert b.daily_loss_pct(100_000.0) is None, (
            "an absent baseline must be None, not zero, or a missing "
            "measurement reads as 'no loss'"
        )


class TestHistoryAndMaintenance:

    def test_observations_append_to_equity_snapshots(self, tracker, journal):
        tracker.observe(100_000.0, balance=100_000.0, now_day="2026-10-04")
        tracker.observe(101_000.0, balance=100_000.0, now_day="2026-10-04")
        rows = journal.query("SELECT * FROM equity_snapshots ORDER BY id")
        assert len(rows) == 2

    def test_history_returns_recent_first(self, tracker):
        tracker.observe(100_000.0, now_day="2026-10-04")
        tracker.observe(101_000.0, now_day="2026-10-04")
        history = tracker.history()
        assert history[0]["equity"] == 101_000.0

    def test_prune_removes_old_snapshots(self, tracker, journal):
        old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat(
            timespec="seconds")
        journal.execute(
            "INSERT INTO equity_snapshots (equity, balance, margin, "
            "open_risk, open_positions, ts) VALUES (100, 100, 0, 0, 0, ?)", (old,))
        tracker.observe(100_000.0, now_day="2026-10-04")
        removed = tracker.prune_history(keep_days=90)
        assert removed == 1
        assert tracker.history()[-1]["equity"] == 100_000.0

    def test_peak_age_is_reported(self, tracker):
        tracker.observe(100_000.0, now_day="2026-10-04")
        age = tracker.peak_age_hours("2026-10-04")
        assert age is not None and age < 0.01

    def test_peak_age_is_none_without_a_baseline(self, tracker):
        assert tracker.peak_age_hours("2026-10-04") is None


class TestGateReceivesBaselines:
    """The point of Phase 3: the gate stops blocking on unknown equity."""

    def test_account_state_from_persisted_baselines(self, tracker, journal):
        from engine.risk import RiskEngine

        baselines = tracker.observe(100_000.0, now_day="2026-10-04")
        risk = RiskEngine(journal)
        state = risk.account_state_from_positions(
            equity=98_000.0,
            start_of_day_equity=baselines.start_of_day_equity,
            peak_equity=baselines.peak_equity,
            open_positions=[],
        )
        assert state.equity is not None
        assert state.start_of_day_equity is not None
        assert state.peak_equity is not None

        # And the drawdown limit is now evaluable rather than blocking.
        specs = {"EURUSD": {"trade_tick_value": 0.8886124316879194,
                            "trade_tick_size": 1e-05}}
        risk.set_symbol_specs(specs)
        intent = risk.build_order_intent(
            symbol="EURUSD", side="BUY", volume=0.10,
            entry_price=1.10000, stop_loss=1.09900)
        decision = risk.evaluate(intent, state)
        reasons = " ".join(decision.reasons)
        assert "peak equity is unknown" not in reasons
        assert "start-of-day equity is unknown" not in reasons, (
            "the baselines must satisfy the gate's data-sufficiency checks"
        )