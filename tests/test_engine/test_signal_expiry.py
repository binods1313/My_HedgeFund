"""Weekend expiry tests.

Expiry = min(bar_close + 900s, signal_created_at + 1800s). No weekend clamping:
a signal whose raw expiry lands on Saturday or Sunday is expired at that
time. The market is closed, so no approval can occur. Forward-clamping to
Monday would extend validity across the weekend, which is forbidden.

Key test case: a signal from a Friday 20:59 UTC bar close expires at
21:14 UTC (20:59 + 15 min). It is expired well before midnight, so it is
unapprovable on Saturday and on the following Monday.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from engine.scan_cycle import (
    ScanCycle,
    _EXPIRY_BAR_WINDOW,
    _EXPIRY_CREATION_WINDOW,
)


def _make_cycle() -> ScanCycle:
    return ScanCycle(symbols=["EURUSD"], interval_seconds=0)


class TestExpiryFormula:
    """Expiry must remain min(bar_close + 900s, created + 1800s)."""

    def test_expiry_is_bar_close_plus_900s(self):
        """When creation is early enough, the bar-close bound dominates."""
        cycle = _make_cycle()
        bar_close = "2026-10-16T20:59:00+00:00"
        created = "2026-10-16T20:59:00+00:00"  # same instant
        expiry = cycle._compute_expiry(bar_close, created)
        expected = datetime(2026, 10, 16, 20, 59, 0, tzinfo=timezone.utc)
        expected_close_bound = expected + timedelta(seconds=_EXPIRY_BAR_WINDOW)
        expected_created_bound = expected + timedelta(seconds=_EXPIRY_CREATION_WINDOW)
        expected_min = min(expected_close_bound, expected_created_bound)
        assert expiry == pytest.approx(expected_min.isoformat())

    def test_creation_bound_is_a_safety_cap(self):
        """min() keeps the earlier bound. A late creation does not replace it.

        Bar close 20:59 and created 22:00: bar+900 (21:14) is earlier than
        created+1800 (22:30). There is no skew fallback to created+1800.
        """
        cycle = _make_cycle()
        bar_close = "2026-10-16T20:59:00+00:00"
        created = "2026-10-16T22:00:00+00:00"  # 61 min after bar close
        expiry = cycle._compute_expiry(bar_close, created)
        close_dt = datetime(2026, 10, 16, 20, 59, 0, tzinfo=timezone.utc)
        expected = close_dt + timedelta(seconds=_EXPIRY_BAR_WINDOW)
        assert expiry == expected.isoformat()

    def test_expiry_source_has_no_skew_fallback(self):
        import inspect
        source = inspect.getsource(ScanCycle._compute_expiry)
        assert "MAX_CLOCK_SKEW" not in source
        assert "falls back" not in source.lower()


class TestFridayExpiry:
    """Friday 20:59 UTC close expires at 21:14 — before midnight."""

    def test_friday_close_expires_within_15_minutes(self):
        """20:59 + 900s = 21:14; this is the expiry (creation bound is 21:29)."""
        cycle = _make_cycle()
        bar_close = "2026-10-16T20:59:00+00:00"  # Friday
        created = "2026-10-16T20:59:00+00:00"
        expiry = cycle._compute_expiry(bar_close, created)
        expiry_dt = datetime.fromisoformat(expiry)
        expected = datetime(2026, 10, 16, 20, 59, 0, tzinfo=timezone.utc)
        assert expiry_dt == expected + timedelta(seconds=_EXPIRY_BAR_WINDOW)
        assert expiry_dt.hour == 21
        assert expiry_dt.minute == 14  # 20:59 + 15 min

    def test_friday_close_expired_by_21_15(self):
        """Signal expires at 21:14; at 21:15 it is already expired."""
        cycle = _make_cycle()
        bar_close = "2026-10-16T20:59:00+00:00"
        created = "2026-10-16T20:59:00+00:00"
        expiry = cycle._compute_expiry(bar_close, created)
        expiry_dt = datetime.fromisoformat(expiry)
        check_time = datetime(2026, 10, 16, 21, 15, 0, tzinfo=timezone.utc)
        assert expiry_dt < check_time, (
            f"expiry {expiry_dt} must be before 21:15; signal should be expired"
        )

    def test_friday_close_unapprovable_on_monday(self):
        """Expiry is Friday 21:14 — long before Monday 00:00."""
        cycle = _make_cycle()
        bar_close = "2026-10-16T20:59:00+00:00"  # Friday 20:59
        created = "2026-10-16T20:59:00+00:00"
        expiry = cycle._compute_expiry(bar_close, created)
        expiry_dt = datetime.fromisoformat(expiry)
        monday = datetime(2026, 10, 19, 9, 30, 0, tzinfo=timezone.utc)
        assert expiry_dt < monday, (
            f"expiry {expiry_dt} must be before Monday; signal must be expired"
        )

    def test_friday_close_already_expired_on_saturday(self):
        """By Saturday morning the signal (exp 21:14 Fri) is expired."""
        cycle = _make_cycle()
        bar_close = "2026-10-16T20:59:00+00:00"
        created = "2026-10-16T20:59:00+00:00"
        expiry = cycle._compute_expiry(bar_close, created)
        expiry_dt = datetime.fromisoformat(expiry)
        saturday = datetime(2026, 10, 17, 8, 0, 0, tzinfo=timezone.utc)
        assert expiry_dt < saturday


class TestNoWeekendClamp:
    """No clamp extends expiry into the next trading week."""

    def test_saturday_expiry_not_clamped_to_monday(self):
        """Signal created Saturday 02:00 (market closed) expires Saturday 02:30,
        NOT pushed to Monday 00:00.
        """
        cycle = _make_cycle()
        bar_close = "2026-10-17T02:00:00+00:00"  # Saturday
        created = "2026-10-17T02:00:00+00:00"
        expiry = cycle._compute_expiry(bar_close, created)
        expiry_dt = datetime.fromisoformat(expiry)
        # Expiry is 02:15 or 02:30 Saturday — definitely NOT Monday
        assert expiry_dt.weekday() == 5, "expiry stays on Saturday, not pushed"
        monday = datetime(2026, 10, 19, 0, 0, 0, tzinfo=timezone.utc)
        assert expiry_dt < monday

    def test_sunday_expiry_not_clamped_to_monday(self):
        """Signal created Sunday 03:00 expires Sunday 03:15, NOT Monday."""
        cycle = _make_cycle()
        bar_close = "2026-10-18T03:00:00+00:00"  # Sunday
        created = "2026-10-18T03:00:00+00:00"
        expiry = cycle._compute_expiry(bar_close, created)
        expiry_dt = datetime.fromisoformat(expiry)
        assert expiry_dt.weekday() == 6, "expiry stays on Sunday, not pushed"
        monday = datetime(2026, 10, 19, 0, 0, 0, tzinfo=timezone.utc)
        assert expiry_dt < monday


class TestUnixTimestampHandling:
    """bar_close_ts may arrive as a Unix timestamp string."""

    def test_weekend_expiry_from_unix_timestamp(self):
        """A Unix-timestamped Friday close must use the same formula."""
        cycle = _make_cycle()
        # Friday 20:59 UTC as Unix timestamp
        bar_close_dt = datetime(2026, 10, 16, 20, 59, 0, tzinfo=timezone.utc)
        bar_close_ts = str(int(bar_close_dt.timestamp()))
        created = "2026-10-16T20:59:00+00:00"
        expiry = cycle._compute_expiry(bar_close_ts, created)
        expiry_dt = datetime.fromisoformat(expiry)
        expected_close_bound = bar_close_dt + timedelta(seconds=_EXPIRY_BAR_WINDOW)
        expected_created_bound = datetime(
            2026, 10, 16, 20, 59, 0, tzinfo=timezone.utc
        ) + timedelta(seconds=_EXPIRY_CREATION_WINDOW)
        expected = min(expected_close_bound, expected_created_bound)
        assert expiry_dt == expected

    def test_clamp_handles_naive_input_as_utc(self):
        """Naive datetime input is treated as UTC."""
        cycle = _make_cycle()
        expiry = cycle._compute_expiry(
            "2026-10-16T20:59:00",   # no tz suffix
            "2026-10-16T20:59:00",
        )
        expiry_dt = datetime.fromisoformat(expiry)
        expected = datetime(2026, 10, 16, 20, 59, 0, tzinfo=timezone.utc)
        expected_close = expected + timedelta(seconds=_EXPIRY_BAR_WINDOW)
        expected_created = expected + timedelta(seconds=_EXPIRY_CREATION_WINDOW)
        assert expiry_dt == min(expected_close, expected_created)


def _expiry_for_offset(offset_hours: int) -> datetime:
    """Friday 20:45 UTC bar open, seen on a broker clock ``offset_hours`` ahead.

    Close is 21:00 UTC. Created is 21:00:05 UTC. Expiry is the bar-close
    bound, 21:15 UTC, not created+1800.
    """
    cycle = _make_cycle()
    real_open = datetime(2026, 10, 16, 20, 45, tzinfo=timezone.utc)
    broker_epoch = int(real_open.timestamp()) + offset_hours * 3600
    assert broker_epoch % 900 == 0
    created = "2026-10-16T21:00:05+00:00"
    expiry = cycle._expiry_from_bar_open(broker_epoch, created, offset_hours * 3600)
    return datetime.fromisoformat(expiry)


class TestBrokerOffsetExpiry:
    """Broker epochs are converted to UTC before the min() formula.

    The old path treated a tick more than 900 s from creation as a reason
    to ignore the bar and use created+1800. That path is gone.
    """

    def test_plus_3h_converts_to_utc_bar_close(self):
        expiry = _expiry_for_offset(3)
        assert expiry == datetime(2026, 10, 16, 21, 15, tzinfo=timezone.utc)
        created = datetime(2026, 10, 16, 21, 0, 5, tzinfo=timezone.utc)
        assert expiry != created + timedelta(seconds=_EXPIRY_CREATION_WINDOW)

    def test_plus_2h_converts_to_utc_bar_close(self):
        expiry = _expiry_for_offset(2)
        assert expiry == datetime(2026, 10, 16, 21, 15, tzinfo=timezone.utc)

    def test_zero_offset_uses_the_epoch_as_utc(self):
        expiry = _expiry_for_offset(0)
        assert expiry == datetime(2026, 10, 16, 21, 15, tzinfo=timezone.utc)

    def test_friday_close_expires_before_saturday(self):
        """An M15 bar that opens Friday 20:45 UTC closes 21:00 and expires 21:15."""
        expiry = _expiry_for_offset(0)
        assert expiry.weekday() == 4
        assert expiry.hour == 21 and expiry.minute == 15
        saturday = datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc)
        monday = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)
        assert expiry < saturday < monday


class TestBrokerOffsetDetection:
    """Offset detection fails closed. DST is the only accepted change."""

    def test_fresh_plus_3h_tick_is_three_hours(self):
        from quote_clock import detect_broker_offset_seconds
        now = 1_700_000_000.0
        assert detect_broker_offset_seconds([int(now) + 10_800 - 2], now) == 10_800

    def test_fresh_plus_2h_and_zero(self):
        from quote_clock import detect_broker_offset_seconds
        now = 1_700_000_000.0
        assert detect_broker_offset_seconds([int(now) + 7_200], now) == 7_200
        assert detect_broker_offset_seconds([int(now) + 5], now) == 0

    def test_stale_tick_is_not_an_offset(self):
        from quote_clock import detect_broker_offset_seconds
        now = 1_700_000_000.0
        stale = int(now) + 10_800 - 86_400
        assert detect_broker_offset_seconds([stale], now) is None

    def test_half_hour_offset_fails_closed(self):
        from quote_clock import detect_broker_offset_seconds
        now = 1_700_000_000.0
        assert detect_broker_offset_seconds([int(now) + 5 * 3600 + 1800], now) is None

    def test_dst_one_hour_step_is_accepted(self):
        cycle = _make_cycle()
        assert cycle._accept_offset(2 * 3600) == 2 * 3600
        assert cycle._accept_offset(3 * 3600) == 3 * 3600
        assert cycle._accepted_offset == 3 * 3600

    def test_two_hour_jump_is_refused(self):
        cycle = _make_cycle()
        assert cycle._accept_offset(0) == 0
        assert cycle._accept_offset(2 * 3600) is None
        assert cycle._accepted_offset == 0
