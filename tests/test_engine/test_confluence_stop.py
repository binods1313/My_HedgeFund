"""Harness stop: beyond the sweep wick, at least max(ATR, broker min)."""
from __future__ import annotations

import pytest

from engine.indicators.structure import Sweep
from scripts.backtest_confluence import BrokerStops, sweep_stop


EUR = BrokerStops(point=0.00001, tick_size=0.00001, stops_level_points=0, spread_points=10)
# broker_min = (0 + 10) * 0.00001 = 0.00010


def _buy(level=1.10000, extreme=0.00020) -> Sweep:
    return Sweep(index=10, timestamp=None, direction="BUY",
                 level=level, extreme=extreme, reclaimed=True)


def _sell(level=1.10000, extreme=0.00020) -> Sweep:
    return Sweep(index=10, timestamp=None, direction="SELL",
                 level=level, extreme=extreme, reclaimed=True)


def test_buy_one_tick_wick_is_pushed_to_one_atr() -> None:
    """Entry 1 tick above the wick must not keep a 1-tick stop."""
    sweep = _buy(level=1.16478, extreme=0.00001)  # wick = 1.16477
    got = sweep_stop("BUY", 1.16477, sweep, atr=0.00100, spec=EUR)
    assert got is not None
    stop, dist, min_dist = got
    assert min_dist == pytest.approx(0.00100)
    assert dist == pytest.approx(0.00100)
    assert stop == pytest.approx(1.16477 - 0.00100)
    assert 1.16477 - stop >= 0.00100 - 1e-12


def test_sell_stop_sits_beyond_the_wick_when_that_is_farther_than_atr() -> None:
    sweep = _sell(level=1.10000, extreme=0.00200)  # wick = 1.10200
    got = sweep_stop("SELL", 1.09900, sweep, atr=0.00050, spec=EUR)
    assert got is not None
    stop, dist, min_dist = got
    # beyond wick by 1 tick: 1.10201; entry+atr = 1.09950; take the farther
    assert stop == pytest.approx(1.10200 + 0.00001)
    assert dist == pytest.approx(stop - 1.09900)
    assert dist >= min_dist


def test_broker_floor_wins_when_larger_than_atr() -> None:
    fat = BrokerStops(point=0.00001, tick_size=0.00001,
                      stops_level_points=80, spread_points=20)
    # broker_min = 100 * 0.00001 = 0.00100; ATR = 0.00020
    sweep = _buy(level=1.10000, extreme=0.00005)
    got = sweep_stop("BUY", 1.10010, sweep, atr=0.00020, spec=fat)
    assert got is not None
    stop, dist, min_dist = got
    assert min_dist == pytest.approx(0.00100)
    assert dist == pytest.approx(0.00100)
    assert stop == pytest.approx(1.10010 - 0.00100)


def test_rejects_missing_sweep_or_nonpositive_atr() -> None:
    sweep = _buy()
    assert sweep_stop("BUY", 1.10, None, atr=0.001, spec=EUR) is None
    assert sweep_stop("BUY", 1.10, sweep, atr=0.0, spec=EUR) is None
    assert sweep_stop("BUY", 1.10, sweep, atr=-0.1, spec=EUR) is None


def test_rejects_when_constructed_stop_is_on_the_wrong_side() -> None:
    # BUY but the swept wick is above the entry — cannot place a long stop.
    sweep = _buy(level=1.10200, extreme=0.00010)  # wick 1.10190
    assert sweep_stop("BUY", 1.10000, sweep, atr=0.00010, spec=EUR) is None
