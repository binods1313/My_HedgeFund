"""Reproduces signal id=4 stop_quality rejection (A1119, 1792614600).

The sweep wick at 0.70304 sits ABOVE entry 0.70234, so
sweep_stop must reject with None.  Before the fix this raised
ValueError instead of returning None.
"""
from __future__ import annotations

import pytest

from engine.indicators.structure import Sweep
from engine.strategy import BrokerStops, sweep_stop


# AUD/USD broker params (10-digit, 5-decimal quotes)
AUD = BrokerStops(point=0.00001, tick_size=0.00001, stops_level_points=0, spread_points=9)
# broker_min = (0 + 9) * 0.00001 = 0.00009


def _make_sweep(level=0.70305, extreme=0.00001) -> Sweep:
    return Sweep(
        index=159,
        timestamp=1792614600,
        direction="BUY",
        level=level,
        extreme=extreme,
        reclaimed=True,
    )


def test_stop_quality_rejection_audusd_id4() -> None:
    """Signal id=4 AUDUSD BUY, bar close 2026-10-21 19:00 UTC (epoch 1792619100).

    Sweep bar epoch 1792614600 (17:30 UTC), low=0.70304 swept a tick
    below pivot low level=0.70305, extreme=1.0e-05 (distance below level).

    wick = level - extreme = 0.70304 >= entry 0.70234 → sweep_stop returns None.
    """
    sweep = _make_sweep(level=0.70305, extreme=0.00001)
    result = sweep_stop("BUY", 0.70234, sweep, atr=0.000373, spec=AUD)
    assert result is None
