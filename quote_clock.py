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


# A broker offset is a whole number of hours. Half-hour zones and stale
# ticks fail closed: the caller must not invent an expiry from them.
_OFFSET_GRID_S = 3600
_OFFSET_RESIDUAL_S = 120
_OFFSET_MIN_S = -12 * 3600
_OFFSET_MAX_S = 14 * 3600


def detect_broker_offset_seconds(server_epochs, utc_now: float) -> int | None:
    """Return broker-server minus UTC, in whole hours, or None.

    MetaTrader stores the server wall clock as a Unix timestamp. A fresh
    tick therefore leads real UTC by the broker offset: a UTC+3 server
    leads by 10800 seconds. The offset is the whole hour nearest to
    ``server_epoch - utc_now``, and only when the leftover is at most
    120 seconds and the hour is inside -12..+14.

    A stale tick, a half-hour offset, or an out-of-range value returns
    None. This function does not remember a previous offset. The caller
    accepts a later reading only when it matches the previous one or
    differs by exactly 3600 seconds (a DST change). Any other jump is
    refused. The first reading after a process start is accepted, so a
    restart is how a real multi-hour change is taken up.
    """
    best: int | None = None
    best_residual: float | None = None
    for epoch in server_epochs:
        try:
            raw = float(epoch) - float(utc_now)
        except (TypeError, ValueError):
            continue
        hours = int(round(raw / _OFFSET_GRID_S))
        rounded = hours * _OFFSET_GRID_S
        residual = abs(raw - rounded)
        if residual > _OFFSET_RESIDUAL_S:
            continue
        if rounded < _OFFSET_MIN_S or rounded > _OFFSET_MAX_S:
            continue
        if best_residual is None or residual < best_residual:
            best = rounded
            best_residual = residual
    return best
