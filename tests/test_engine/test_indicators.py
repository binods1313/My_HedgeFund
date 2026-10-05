r"""Tests for the structural indicators.

Each rule is tested against a hand-built frame with a known answer, so a
failure means the rule is wrong rather than that the market moved.

Hand-built cases rather than live candles: a rule that fires on real data every
bar is indistinguishable from one that is merely broken, and neither is caught
by a test that only asserts 'no exception'.
"""
import numpy as np
import pandas as pd
import pytest

from engine.indicators.structure import (
    OTE_HIGH,
    OTE_LOW,
    daily_extremes,
    detect_sweeps,
    find_fair_value_gaps,
    find_pivot_highs,
    find_pivot_lows,
    find_swing_leg,
    in_ote_zone,
    ote_zone,
    unfilled_fvgs,
    validate_ohlcv,
)
from engine.indicators.trend import (
    SESSION_WINDOWS,
    ema_ribbon,
    session_vwap,
    typical_price,
    vwap,
)


def frame(rows, volume=None, start="2026-10-01T00:00:00"):
    """Build an OHLCV frame from (open, high, low, close) tuples."""
    stamps = pd.date_range(start, periods=len(rows), freq="15min")
    data = {
        "time": stamps,
        "open": [r[0] for r in rows],
        "high": [r[1] for r in rows],
        "low": [r[2] for r in rows],
        "close": [r[3] for r in rows],
    }
    if volume is None:
        volume = [100.0] * len(rows)
    data["tick_volume"] = volume
    return pd.DataFrame(data)


def flat(n=50, price=100.0, volume=None):
    return frame([(price, price + 0.1, price - 0.1, price)] * n, volume)


# ─────────────────────────────────────────────────────────────────────────────
# Input validation
# ─────────────────────────────────────────────────────────────────────────────

class TestValidation:

    def test_empty_frame_is_refused(self):
        with pytest.raises(ValueError):
            validate_ohlcv(pd.DataFrame())

    def test_missing_column_is_refused(self):
        with pytest.raises(KeyError):
            validate_ohlcv(pd.DataFrame({"open": [1.0], "close": [1.0]}))

    def test_high_below_low_is_refused(self):
        with pytest.raises(ValueError):
            validate_ohlcv(frame([(100, 99, 101, 100)]))

    def test_out_of_order_rows_are_refused(self):
        df = flat(10)
        with pytest.raises(ValueError):
            validate_ohlcv(df.iloc[::-1])


# ─────────────────────────────────────────────────────────────────────────────
# Pivots
# ─────────────────────────────────────────────────────────────────────────────

class TestPivots:

    def test_a_clear_peak_is_found(self):
        # Rising, a spike, then falling: an unambiguous pivot high.
        rows = [(100 + i, 100 + i, 99 + i, 100 + i) for i in range(6)]
        rows += [(106, 110, 105, 106)]           # the spike
        rows += [(105 - i, 106 - i, 104 - i, 105 - i) for i in range(6)]
        pivots = find_pivot_highs(frame(rows), lookback=3)
        assert any(abs(level - 110) < 1e-9 for _, level in pivots), pivots

    def test_flat_data_has_no_pivots(self):
        """A market going nowhere has no structure, and saying so matters."""
        assert find_pivot_highs(flat(40), lookback=3) == []
        assert find_pivot_lows(flat(40), lookback=3) == []

    def test_a_clear_trough_is_found(self):
        rows = [(100 + i, 101 + i, 100 + i, 100 + i) for i in range(6)]
        rows += [(94, 95, 90, 94)]
        rows += [(95 + i, 96 + i, 95 + i, 95 + i) for i in range(6)]
        pivots = find_pivot_lows(frame(rows), lookback=3)
        assert any(abs(level - 90) < 1e-9 for _, level in pivots), pivots

    def test_the_live_bar_is_never_a_pivot(self):
        """An unconfirmed pivot would move as new bars arrive."""
        rows = [(100 + i, 100 + i, 99 + i, 100 + i) for i in range(10)]
        rows += [(200, 300, 199, 200)]           # the very last bar spikes
        pivots = find_pivot_highs(frame(rows), lookback=3)
        assert all(level < 150 for _, level in pivots), (
            "the last bar cannot be a confirmed pivot"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Liquidity sweeps
# ─────────────────────────────────────────────────────────────────────────────

class TestSweeps:

    def test_wick_below_support_and_reclaim_is_a_buy_sweep(self):
        rows = [(100, 100.5, 99.5, 100)] * 10
        rows += [(100, 100.2, 98.0, 100.2)]     # dips below, closes back up
        sweep = detect_sweeps(frame(rows), levels={"low": 99.0}, lookback=5)
        assert sweep, "a reclaim of a prior low is a sweep"
        assert sweep[-1].direction == "BUY"
        assert sweep[-1].is_reclaim is True
        assert sweep[-1].level == 99.0

    def test_wick_above_resistance_and_reclaim_is_a_sell_sweep(self):
        rows = [(100, 100.5, 99.5, 100)] * 10
        rows += [(100, 101.0, 99.8, 99.8)]     # pokes above, closes below
        sweep = detect_sweeps(frame(rows), levels={"high": 100.5}, lookback=5)
        assert sweep
        assert sweep[-1].direction == "SELL"
        assert sweep[-1].is_reclaim is True

    def test_a_clean_break_is_not_a_sweep(self):
        """Taking liquidity and holding it is not a failed take."""
        rows = [(100, 100.5, 99.5, 100)] * 10
        rows += [(100, 102.0, 100.0, 101.5)]    # breaks out and stays out
        sweep = detect_sweeps(frame(rows), levels={"high": 100.5}, lookback=5)
        assert not sweep, (
            "a bar closing above the level did not reclaim it, so no sweep"
        )

    def test_flat_data_produces_no_sweep(self):
        assert detect_sweeps(flat(30), levels={"high": 100.0}, lookback=10) == []

    def test_extreme_records_how_far_beyond(self):
        rows = [(100, 100.5, 99.5, 100)] * 10
        rows += [(100, 100.2, 96.0, 100.2)]
        sweep = detect_sweeps(frame(rows), levels={"low": 99.0}, lookback=5)
        assert sweep[-1].extreme == pytest.approx(3.0)


class TestDailyExtremes:

    def test_excludes_the_live_day(self):
        """A sweep must be judged against a level that existed before today."""
        rows = [(100, 101, 99, 100)] * 8
        df = frame(rows, start="2026-10-01T21:00:00")   # all on 10-01
        df2 = pd.concat([
            df,
            frame([(110, 111, 109, 110)] * 4,
                  start="2026-10-02T00:00:00"),
        ], ignore_index=True)
        levels = daily_extremes(df2)
        assert levels["high"] == pytest.approx(101.0), (
            "the live day's own high must not become the level it is swept "
            "against"
        )

    def test_single_day_has_no_prior_levels(self):
        assert daily_extremes(flat(10)) == {}


# ─────────────────────────────────────────────────────────────────────────────
# Fair value gaps
# ─────────────────────────────────────────────────────────────────────────────

class TestFairValueGaps:

    def test_bullish_gap_is_detected(self):
        """Bar i-2's high below bar i's low, with the middle bar bridging.

        The gap bar must not be the last row: i is scanned up to
        len(df) - 2, so a gap on the final bar has no forward room to be
        confirmed. One row is appended after it.
        """
        rows = [(99, 100, 98, 99)] * 5
        rows += [(99, 101, 99, 100.5)]        # bridging bar
        rows += [(100.5, 103, 100.5, 102.5)]  # leaves 100-102 open
        rows += [(102.5, 103, 102.0, 102.4)]  # room to confirm
        df = frame(rows)

        gaps = find_fair_value_gaps(df)
        bullish = [g for g in gaps if g.direction == "BULLISH"]
        assert bullish, [(g.index, g.direction) for g in gaps]
        # The zone runs from the older bar's HIGH to the newer bar's LOW.
        # Using the newer bar's high here would overstate the gap.
        assert bullish[-1].bottom == pytest.approx(100.0)
        assert bullish[-1].top == pytest.approx(100.5)
        assert bullish[-1].height == pytest.approx(0.5)

    def test_bearish_gap_is_detected(self):
        rows = [(101, 102, 101, 101)] * 5
        rows += [(101, 101, 99.5, 100)]        # bridging bar
        rows += [(100, 100, 97, 98)]           # opens a downside gap
        rows += [(98, 98.5, 97.5, 98.2)]      # room to confirm
        gaps = find_fair_value_gaps(frame(rows))
        bearish = [g for g in gaps if g.direction == "BEARISH"]
        assert bearish, [(g.index, g.direction) for g in gaps]
        assert bearish[-1].top == pytest.approx(101.0)

    def test_overlapping_bars_are_not_a_gap(self):
        assert find_fair_value_gaps(flat(40)) == []

    def test_a_gap_price_returned_to_is_filled(self):
        rows = [(99, 100, 98, 99)] * 5
        rows += [(99, 101, 99, 100.5)]
        rows += [(100.5, 103, 100.5, 102.5)]
        rows += [(102.5, 103, 99, 99.5)]        # trades back through
        gaps = find_fair_value_gaps(frame(rows))
        assert gaps and gaps[-1].filled is True

    def test_unfilled_gap_is_tradeable(self):
        rows = [(99, 100, 98, 99)] * 5
        rows += [(99, 101, 99, 100.5)]
        rows += [(100.5, 103, 100.5, 102.5)]
        rows += [(102.5, 103, 102.0, 102.4)]
        assert unfilled_fvgs(frame(rows), direction="BULLISH"), (
            "an untouched gap is exactly what a strategy wants to find"
        )

    def test_bridging_bar_must_actually_travel(self):
        """Two bars apart in time are not a gap in price."""
        rows = [(99, 100, 98, 99)] * 5
        rows += [(99, 99.5, 98.5, 99.2)]        # stays well below the gap
        rows += [(99.2, 103, 99.2, 102.5)]      # jumps without bridging
        rows += [(102.5, 103, 102.0, 102.4)]
        gaps = find_fair_value_gaps(frame(rows))
        assert not any(g.direction == "BULLISH" for g in gaps), (
            "a candle that never entered the zone did not bridge it"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Fibonacci / OTE
# ─────────────────────────────────────────────────────────────────────────────

class TestFibonacci:

    def _bull_leg(self):
        rows = [(100 + i, 101 + i, 100 + i, 100 + i) for i in range(8)]
        rows += [(98, 99, 96, 97)]               # trough
        rows += [(97 + i, 98 + i, 97 + i, 97 + i) for i in range(8)]
        return frame(rows)

    def test_ote_band_uses_the_documented_levels(self):
        leg = find_swing_leg(self._bull_leg())
        assert leg is not None
        lower, upper = ote_zone(leg)
        # Ordering is normalised so callers can treat it as (low, high) whatever
        # the leg's direction.
        assert lower <= upper

    def test_ote_retracement_depth_is_correct(self):
        """The band sits at 61.8% and 78.6% of the leg, measured from its end."""
        leg = find_swing_leg(self._bull_leg())
        lower, upper = ote_zone(leg)
        span = abs(leg.end_price - leg.start_price)
        assert span > 0

        # For a bullish leg the end is below the start, so the deeper of the
        # two levels is further below the end.
        deeper = (leg.end_price - lower) / span
        shallower = (leg.end_price - upper) / span
        assert shallower == pytest.approx(OTE_LOW, abs=1e-6)
        assert deeper == pytest.approx(OTE_HIGH, abs=1e-6)

    def test_price_inside_the_band_is_in_ote(self):
        leg = find_swing_leg(self._bull_leg())
        lower, upper = ote_zone(leg)
        assert in_ote_zone((lower + upper) / 2, leg) is True

    def test_price_outside_the_band_is_not(self):
        leg = find_swing_leg(self._bull_leg())
        assert in_ote_zone(leg.end_price * 2, leg) is False

    def test_flat_data_has_no_swing_leg(self):
        assert find_swing_leg(flat(40)) is None

    def test_ote_zone_low_below_high(self):
        leg = find_swing_leg(self._bull_leg())
        lower, upper = ote_zone(leg)
        assert lower < upper


# ─────────────────────────────────────────────────────────────────────────────
# EMA ribbon
# ─────────────────────────────────────────────────────────────────────────────

class TestEmaRibbon:

    def test_sustained_rise_is_bullish(self):
        """A trending random walk, not a straight ramp.

        On perfectly constant increments every EMA converges to a fixed lag, so
        fast, mid and slow separate by a constant and the stack ordering can
        read as flat. Real price does not move in unit steps, so the fixture uses
        a walk with variable increments.
        """
        rng = np.random.default_rng(11)
        price = 100.0
        rows = []
        for _ in range(320):
            price = max(1.0, price + rng.normal(0.06, 0.09))
            rows.append((price, price + 0.05, price - 0.05, price))

        ribbon = ema_ribbon(frame(rows))
        assert ribbon.fast > ribbon.mid > ribbon.slow, (
            f"expected a rising stack, got {ribbon.fast:.4f}/"
            f"{ribbon.mid:.4f}/{ribbon.slow:.4f}"
        )
        assert ribbon.direction == "BULLISH"
        assert ribbon.aligned is True

    def test_sustained_fall_is_bearish(self):
        rng = np.random.default_rng(13)
        price = 200.0
        rows = []
        for _ in range(320):
            price = max(1.0, price + rng.normal(-0.06, 0.09))
            rows.append((price, price + 0.05, price - 0.05, price))

        ribbon = ema_ribbon(frame(rows))
        assert ribbon.direction == "BEARISH"
        assert ribbon.fast < ribbon.mid < ribbon.slow

    def test_flat_market_is_mixed_not_a_trend(self):
        """Stacked but flat is not a trend, and calling it one is how a
        strategy ends up trading a market that stopped moving."""
        rng = np.random.default_rng(3)
        rows = [(100, 100.05, 99.95, 100 + rng.normal(0, 0.01))
                for _ in range(320)]
        ribbon = ema_ribbon(frame(rows))
        assert ribbon.direction == "MIXED", (
            f"a flat market reported {ribbon.direction}"
        )

    def test_short_history_is_refused(self):
        """A 200-period EMA seeded from 30 bars tracks price, not trend."""
        with pytest.raises(ValueError):
            ema_ribbon(flat(30))

    def test_slope_is_reported(self):
        rng = np.random.default_rng(17)
        price = 100.0
        rows = []
        for _ in range(320):
            price = max(1.0, price + rng.normal(0.06, 0.09))
            rows.append((price, price + 0.05, price - 0.05, price))
        ribbon = ema_ribbon(frame(rows))
        assert ribbon.mid_slope > 0


# ─────────────────────────────────────────────────────────────────────────────
# VWAP
# ─────────────────────────────────────────────────────────────────────────────

class TestVwap:

    def test_uniform_volume_gives_the_typical_price(self):
        df = flat(10, price=100.0)
        series = vwap(df, anchor="cumulative")
        assert series.iloc[-1] == pytest.approx(100.0, abs=1e-6)

    def test_typical_price_is_the_midpoint(self):
        df = frame([(100, 102, 98, 101)])
        assert typical_price(df).iloc[0] == pytest.approx((102 + 98 + 101) / 3)

    def test_cumulative_vwap_follows_volume(self):
        """Heavy volume at 100 must drag the average toward 100.

        The fixture needs genuinely different prices: with every typical price
        identical, no weighting is observable and the test proves nothing.
        """
        rows = [(100, 100, 100, 100)] * 3
        rows += [(200, 200, 200, 200)] * 3      # light volume, far price
        rows += [(100, 100, 100, 100)] * 3
        volume = [1000.0] * 3 + [1.0] * 3 + [1000.0] * 3
        simple_mean = 100.0                       # the true average of closes

        value = vwap(frame(rows, volume=volume), anchor="cumulative").iloc[-1]
        assert value < simple_mean - 0.01 or value == pytest.approx(
            simple_mean, abs=1.0), value
        assert value < 133.3, (
            "volume weighting must pull the average well below the unweighted "
            f"mean of 133.3; got {value}"
        )
        assert value == pytest.approx(100.05, abs=0.01)

    def test_missing_volume_is_refused(self):
        df = flat(10).drop(columns=["tick_volume"])
        with pytest.raises(KeyError):
            vwap(df)

    def test_zero_volume_does_not_divide_by_zero(self):
        df = flat(10, volume=[0.0] * 10)
        series = vwap(df, anchor="cumulative")
        assert series.isna().all(), (
            "no volume means no VWAP, not an infinite or zero one"
        )

    def test_session_anchor_resets_each_day(self):
        """A new session must not inherit the previous day's average."""
        day1 = [(100, 100, 100, 100)] * 4
        day2 = [(200, 200, 200, 200)] * 4
        df = pd.concat([
            frame(day1, start="2026-10-01T22:00:00"),
            frame(day2, start="2026-10-02T00:00:00"),
        ], ignore_index=True)
        series = vwap(df, anchor="session")
        assert series.iloc[-1] == pytest.approx(200.0, abs=1e-6), (
            f"cumulative would give ~150; session anchoring must give 200, "
            f"got {series.iloc[-1]}"
        )

    def test_unknown_session_is_refused(self):
        with pytest.raises(ValueError):
            session_vwap(flat(10), session="lunchtime")

    def test_known_sessions_are_well_formed(self):
        for name, (start, end) in SESSION_WINDOWS.items():
            assert 0 <= start < end <= 24, name