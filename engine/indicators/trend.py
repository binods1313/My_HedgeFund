"""Trend and volume context: EMA ribbon and session VWAP.

The structural blocks in structure.py describe where price went. These describe
the backdrop it moved through, so a structural signal is only acted on when the
macro context agrees.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from engine.indicators.structure import validate_ohlcv

#: Session windows in UTC hours. Which session matters depends on the pair:
#: London dominates EUR and GBP, New York dominates USD and indices, and
#: Asian hours set the range most later sessions trade into. The default covers
#: the overlap where most institutional flow happens.
SESSION_WINDOWS: dict[str, tuple[int, int]] = {
    "asian": (0, 8),
    "london": (7, 16),
    "newyork": (12, 21),
    "overlap": (12, 16),
}


@dataclass(frozen=True)
class EmaRibbon:
    """Slope and alignment of the macro EMAs."""

    fast: float
    mid: float
    slow: float
    fast_slope: float          # change over the sample window
    mid_slope: float
    slow_slope: float
    aligned: bool              # fast > mid > slow (bullish) or the reverse
    direction: str             # 'BULLISH' | 'BEARISH' | 'MIXED'
    price_above_fast: bool
    price_below_slow: bool


def ema_ribbon(df: pd.DataFrame, fast: int = 20, mid: int = 50, slow: int = 200,
               slope_window: int = 5) -> EmaRibbon:
    """EMA stack with slope measured over `slope_window` bars.

    Slope matters more than level: EMAs stacked but flat describe a market that
    has stopped trending, which is not the same as a trend.
    """
    validate_ohlcv(df)
    close = _column(df, "close")

    # Spans must be distinct and ordered. Two identical EMAs make
    # 'fast > mid > slow' unsatisfiable, so the ribbon would report MIXED on a
    # clean uptrend -- a silent wrong answer rather than a wrong answer.
    spans = [fast, mid, slow]
    if len(set(spans)) != len(spans):
        raise ValueError(
            f"ema spans must differ; got fast={fast}, mid={mid}, slow={slow}. "
            f"Identical spans collapse the ribbon and report no trend."
        )
    if not (fast < mid < slow):
        raise ValueError(
            f"ema spans must increase fast < mid < slow; got "
            f"fast={fast}, mid={mid}, slow={slow}"
        )

    # Enough history for the slow EMA to settle. Below this the stack is not
    # meaningful: a 200-period EMA seeded from 20 bars tracks the price rather
    # than the trend, and would read as flat in any direction.
    minimum = slow + slope_window + 1
    if len(df) < minimum:
        raise ValueError(
            f"ema_ribbon needs at least {minimum} bars for slow={slow} and "
            f"slope_window={slope_window}; got {len(df)}. A ribbon computed on "
            f"a short sample would misreport the trend."
        )

    def series(span: int) -> pd.Series:
        return close.ewm(span=span, adjust=False).mean()

    f, m, s = series(fast), series(mid), series(slow)
    fv, mv, sv = float(f.iloc[-1]), float(m.iloc[-1]), float(s.iloc[-1])

    def slope(values: pd.Series) -> float:
        window = min(slope_window, len(values) - 1)
        if window < 1:
            return 0.0
        return float(values.iloc[-1] - values.iloc[-(window + 1)])

    fs, ms, ss = slope(f), slope(m), slope(s)
    price = float(close.iloc[-1])

    bullish_stack = fv > mv > sv
    bearish_stack = fv < mv < sv
    rising = fs > 0 and ms > 0
    falling = fs < 0 and ms < 0

    if bullish_stack and rising:
        direction = "BULLISH"
    elif bearish_stack and falling:
        direction = "BEARISH"
    else:
        direction = "MIXED"

    return EmaRibbon(
        fast=fv, mid=mv, slow=sv,
        fast_slope=fs, mid_slope=ms, slow_slope=ss,
        aligned=bullish_stack or bearish_stack,
        direction=direction,
        price_above_fast=price > fv,
        price_below_slow=price < sv,
    )


def typical_price(df: pd.DataFrame) -> pd.Series:
    return (_column(df, "high") + _column(df, "low")
            + _column(df, "close")) / 3.0


def vwap(df: pd.DataFrame, volume_col: str = "tick_volume",
          anchor: str = "session") -> pd.Series:
    """Volume Weighted Average Price, anchored per session by default.

    Anchor choices:
      'session'  resets each UTC day. This is what an institutional VWAP means:
                 the day's volume-weighted fair value.
      'cumulative' never resets. Useful for multi-session context.

    Uses tick_volume, which every MT5 symbol provides. Real traded volume is not
    available on most retail CFD feeds, so this is a proxy; a strategy claiming
    to use true volume on this data would be wrong.
    """
    validate_ohlcv(df)
    if volume_col not in df.columns:
        raise KeyError(
            f"column {volume_col!r} is required for VWAP; got "
            f"{sorted(df.columns)}"
        )
    price = typical_price(df)
    volume = _column(df, volume_col).astype(float)

    if anchor == "cumulative":
        return (price * volume).cumsum() / _safe_cumsum(volume)

    if "time" in df.columns:
        day = pd.to_datetime(df["time"]).dt.date
    else:
        day = pd.to_datetime(df.index).date

    pv = price * volume
    grouped = pv.groupby(day).cumsum()
    volume_grouped = volume.groupby(day).cumsum()
    return grouped / volume_grouped.replace(0.0, np.nan)


def session_vwap(df: pd.DataFrame, session: str,
                  volume_col: str = "tick_volume") -> pd.Series:
    """VWAP anchored to a named session window.

    The window is applied to every day in the frame, so the series is
    continuous while each day's component is reset.
    """
    if session not in SESSION_WINDOWS:
        raise ValueError(
            f"unknown session {session!r}; known: "
            f"{sorted(SESSION_WINDOWS)}"
        )
    start_hour, end_hour = SESSION_WINDOWS[session]

    if "time" in df.columns:
        stamps = pd.to_datetime(df["time"])
    else:
        stamps = pd.to_datetime(df.index)

    price = typical_price(df)
    volume = _column(df, volume_col).astype(float)
    day = stamps.dt.date
    hour = stamps.dt.hour

    in_session = (hour >= start_hour) & (hour < end_hour)
    pv = (price * volume).where(in_session, 0.0)
    vol = volume.where(in_session, 0.0)

    pv_day = pv.groupby(day).cumsum()
    vol_day = vol.groupby(day).cumsum()
    return pv_day / vol_day.replace(0.0, np.nan)


def _safe_cumsum(values: pd.Series) -> pd.Series:
    total = values.cumsum()
    return total.replace(0.0, np.nan)


def _column(df: pd.DataFrame, name: str) -> pd.Series:
    if name not in df.columns:
        raise KeyError(f"column {name!r} is required")
    return df[name]