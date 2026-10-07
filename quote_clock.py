"""Broker tick age. MetaTrader 5 is not imported here.

``tick.time`` is seconds since the epoch on the broker server clock, not UTC.
A UTC+3 FX server (EET/EEST) makes a fresh tick look about three hours in the
future, which is the negative data-age the header was showing. IST is
UTC+5:30 and is not snapped onto a whole hour.
"""

from __future__ import annotations

_FRESH_S = 15 * 60


def broker_tick_age_seconds(tick_time: float, now: float) -> float:
    """Return a tick's age in seconds. The result is never negative.

    A non-negative raw age is returned as-is. A future tick is snapped to the
    nearest whole hour from 1 to 14 only when the leftover age is between 0
    and 15 minutes. Any other future skew, including a few seconds and a
    half-hour offset, is clamped to 0.
    """
    raw = float(now) - float(tick_time)
    if raw >= 0:
        return raw
    ahead = -raw
    hours = int(round(ahead / 3600.0))
    if 1 <= hours <= 14:
        remainder = hours * 3600.0 - ahead
        if 0 <= remainder <= _FRESH_S:
            return remainder
    return 0.0
