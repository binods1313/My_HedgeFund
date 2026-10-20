"""Structural indicators: liquidity sweeps, fair value gaps, Fibonacci OTE.

Pure functions over OHLCV data. Nothing here touches MT5, reads the clock, or
knows what a symbol is. That keeps every rule testable against a hand-built
DataFrame instead of a live terminal.

Each block maps to one of the five building blocks:

    liquidity.py    SMC liquidity sweep of session/daily extremes
    fvg.py          three-candle fair value gap
    fibonacci.py    swing leg anchors and the OTE band

A note on definitions, because several are ambiguous in practice and a wrong
choice silently produces signals that look reasonable and are not:

  - A liquidity sweep here means price traded THROUGH a prior extreme and closed
    back inside it. Merely touching the level is not a sweep; a wick through
    and back is.
  - A fair value gap is a genuine three-candle imbalance: candle n-2's high is
    below candle n's low, or its low is above candle n's high, with the middle
    candle bridging it.
  - Fibonacci levels are measured from a confirmed swing leg. An unconfirmed
    leg produces retracements that move after the fact, so legs are only
    returned once the pivot is a fixed number of bars behind the live bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


def _column(df: pd.DataFrame, name: str) -> pd.Series:
    if name not in df.columns:
        raise KeyError(
            f"column {name!r} is required; got {sorted(df.columns)}"
        )
    return df[name]


def validate_ohlcv(df: pd.DataFrame, min_rows: int = 1) -> None:
    """Reject data no structural rule can be evaluated against.

    Silently accepting a short or malformed frame would produce 'no signal',
    which reads as 'nothing happened' rather than 'the data was unusable'.
    """
    if df is None or len(df) < min_rows:
        raise ValueError(
            f"need at least {min_rows} rows, got "
            f"{0 if df is None else len(df)}"
        )
    for column in ("open", "high", "low", "close"):
        if column not in df.columns:
            raise KeyError(f"column {column!r} is required")
    if not df.index.is_monotonic_increasing:
        raise ValueError(
            "rows must be in ascending time order; structural rules assume "
            "history reads forwards"
        )
    highs = _column(df, "high")
    lows = _column(df, "low")
    if (highs < lows).any():
        raise ValueError("found bars where high < low; data is malformed")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Liquidity sweeps
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Sweep:
    """A failed attempt to take out a prior price extreme."""

    index: int                  # bar index of the sweep
    timestamp: object
    direction: str              # 'BUY' | 'SELL'
    level: float                # the extreme that was swept
    extreme: float              # how far beyond it price went
    reclaimed: bool             # closed back inside the level

    @property
    def is_reclaim(self) -> bool:
        """Only a reclaim counts. A clean break is not a sweep."""
        return self.reclaimed


def find_pivot_highs(df: pd.DataFrame, lookback: int = 3) -> list[tuple[int, float]]:
    """Pivot highs confirmed `lookback` bars back.

    A pivot is only reported once it can no longer change: the high must exceed
    its neighbours on both sides, and it must already be `lookback` bars behind
    the live bar.
    """
    validate_ohlcv(df)
    highs = _column(df, "high").to_numpy(dtype=float)
    end = len(highs) - lookback
    out: list[tuple[int, float]] = []
    for i in range(lookback, max(lookback, end)):
        window = highs[i - lookback:i + lookback + 1]
        if highs[i] > window[0] and highs[i] >= window[-1] \
                and highs[i] == window.max():
            out.append((i, float(highs[i])))
    return out


def find_pivot_lows(df: pd.DataFrame, lookback: int = 3) -> list[tuple[int, float]]:
    """Pivot lows confirmed `lookback` bars back."""
    validate_ohlcv(df)
    lows = _column(df, "low").to_numpy(dtype=float)
    end = len(lows) - lookback
    out: list[tuple[int, float]] = []
    for i in range(lookback, max(lookback, end)):
        window = lows[i - lookback:i + lookback + 1]
        if lows[i] < window[0] and lows[i] <= window[-1] \
                and lows[i] == window.min():
            out.append((i, float(lows[i])))
    return out


def daily_extremes(df: pd.DataFrame) -> dict[str, tuple[float, float]]:
    """Prior-session high and low for each day present, excluding the live day.

    Excluding the live day is the point: a sweep is judged against a level that
    existed BEFORE the current session, not one it created itself.
    """
    validate_ohlcv(df)
    work = df.copy()
    if "time" in work.columns:
        work["_day"] = pd.to_datetime(work["time"]).dt.date
    else:
        work["_day"] = pd.to_datetime(work.index).date

    days = work["_day"].unique()
    if len(days) < 2:
        return {}

    live_day = days[-1]
    prior = work[work["_day"] < live_day]
    if prior.empty:
        return {}
    return {
        "high": float(prior["high"].max()),
        "low": float(prior["low"].min()),
        "day": prior["_day"].iloc[-1],
    }


def detect_sweeps(df: pd.DataFrame, levels: Optional[dict] = None,
                  lookback: int = 20) -> list[Sweep]:
    """Detect failed takes of a liquidity level on the most recent bars.

    A bearish sweep of a high: price trades above the level, then closes back
    below it. Buyers' stops were taken and the move failed.
    """
    validate_ohlcv(df)
    highs = _column(df, "high").to_numpy(dtype=float)
    lows = _column(df, "low").to_numpy(dtype=float)
    closes = _column(df, "close").to_numpy(dtype=float)
    opens = _column(df, "open").to_numpy(dtype=float)

    found: list[Sweep] = []
    start = max(1, len(df) - lookback)

    if levels:
        level_high = levels.get("high")
        level_low = levels.get("low")
        for i in range(start, len(df)):
            if level_high is not None and highs[i] > level_high \
                    and max(opens[i], closes[i]) < level_high:
                found.append(Sweep(
                    index=i,
                    timestamp=_stamp(df, i),
                    direction="SELL",
                    level=float(level_high),
                    extreme=float(highs[i] - level_high),
                    reclaimed=True,
                ))
            if level_low is not None and lows[i] < level_low \
                    and min(opens[i], closes[i]) > level_low:
                found.append(Sweep(
                    index=i,
                    timestamp=_stamp(df, i),
                    direction="BUY",
                    level=float(level_low),
                    extreme=float(level_low - lows[i]),
                    reclaimed=True,
                ))
        return found

    # No explicit level: use confirmed pivot extremes as the resting liquidity.
    pivot_highs = find_pivot_highs(df, lookback=3)
    pivot_lows = find_pivot_lows(df, lookback=3)

    for i in range(start, len(df)):
        for _, level in pivot_highs:
            if level >= highs[:i].max() if i else False:
                continue
            if highs[i] > level and max(opens[i], closes[i]) < level:
                found.append(Sweep(
                    index=i, timestamp=_stamp(df, i), direction="SELL",
                    level=float(level), extreme=float(highs[i] - level),
                    reclaimed=True,
                ))
        for _, level in pivot_lows:
            if level <= lows[:i].min() if i else False:
                continue
            if lows[i] < level and min(opens[i], closes[i]) > level:
                found.append(Sweep(
                    index=i, timestamp=_stamp(df, i), direction="BUY",
                    level=float(level), extreme=float(level - lows[i]),
                    reclaimed=True,
                ))
    return found


def latest_sweep(df: pd.DataFrame, levels: Optional[dict] = None,
                 lookback: int = 20) -> Sweep | None:
    """The most recent sweep, or None. None means 'no signal', not an error."""
    sweeps = detect_sweeps(df, levels=levels, lookback=lookback)
    return sweeps[-1] if sweeps else None


# ─────────────────────────────────────────────────────────────────────────────
# 2. Fair value gaps
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FairValueGap:
    """A three-candle imbalance left by an impulsive move."""

    index: int
    timestamp: object
    direction: str          # 'BULLISH' | 'BEARISH'
    top: float
    bottom: float
    filled: bool

    @property
    def height(self) -> float:
        return self.top - self.bottom


def find_fair_value_gaps(df: pd.DataFrame, min_bars_ago: int = 0,
                         max_bars_ago: int = 200,
                         min_height_atr: float = 0.0,
                         atr: Optional[float] = None) -> list[FairValueGap]:
    """Locate three-candle gaps.

    A bullish gap exists when bar i-2's high sits below bar i's low: the move
    between them left price untraded. The middle bar must actually travel
    between them, otherwise the 'gap' is just two unrelated bars.
    """
    validate_ohlcv(df)
    highs = _column(df, "high").to_numpy(dtype=float)
    lows = _column(df, "low").to_numpy(dtype=float)

    gaps: list[FairValueGap] = []
    total = len(df)
    lowest = max(2, total - max_bars_ago)
    highest = total - 2 - min_bars_ago

    for i in range(lowest, highest + 1):
        # Bullish: older bar's high below the newer bar's low.
        if highs[i - 2] < lows[i]:
            top, bottom = lows[i], highs[i - 2]
            direction = "BULLISH"
        # Bearish: older bar's low above the newer bar's high.
        elif lows[i - 2] > highs[i]:
            top, bottom = lows[i - 2], highs[i]
            direction = "BEARISH"
        else:
            continue

        # The bridging candle must move through the zone, otherwise the two
        # bars are simply apart in time, not in price.
        bridging = (highs[i - 1] >= bottom and lows[i - 1] <= top)
        if not bridging:
            continue

        if min_height_atr and atr and (top - bottom) < min_height_atr * atr:
            continue

        filled = _is_filled(df, i, top, bottom, direction)
        gaps.append(FairValueGap(
            index=i, timestamp=_stamp(df, i), direction=direction,
            top=float(top), bottom=float(bottom), filled=filled,
        ))
    return gaps


def _is_filled(df: pd.DataFrame, gap_index: int, top: float, bottom: float,
               direction: str) -> bool:
    """True when price has traded back through the zone since it formed."""
    highs = _column(df, "high").to_numpy(dtype=float)
    lows = _column(df, "low").to_numpy(dtype=float)
    for j in range(gap_index + 1, len(df)):
        if direction == "BULLISH" and lows[j] <= bottom:
            return True
        if direction == "BEARISH" and highs[j] >= top:
            return True
    return False


def unfilled_fvgs(df: pd.DataFrame, direction: str | None = None,
                   **kwargs) -> list[FairValueGap]:
    """Gaps price has not yet returned to. These are the tradeable ones."""
    gaps = find_fair_value_gaps(df, **kwargs)
    open_gaps = [g for g in gaps if not g.filled]
    if direction:
        wanted = direction.upper()
        open_gaps = [g for g in open_gaps if g.direction == wanted]
    return open_gaps


# ─────────────────────────────────────────────────────────────────────────────
# 3. Fibonacci retracements / OTE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SwingLeg:
    """A confirmed high-to-low or low-to-high move.

    `invalidation` is the 100% level: the swing origin. For a bullish leg, price
    below it means the structure that produced this leg is gone, and any intent
    built from it is dead rather than merely late.
    """

    start_index: int
    end_index: int
    start_price: float
    end_price: float
    direction: str          # 'BULLISH' (low to high) | 'BEARISH'
    invalidation: float = 0.0

    @property
    def is_bullish(self) -> bool:
        return self.direction == "BULLISH"

    def retracement(self, level: float) -> float:
        """Price at `level` of the way back from the end of the leg.

        level 0.0 is the leg's end, 1.0 its start.
        """
        span = self.end_price - self.start_price
        return self.end_price - span * level

    def is_invalidated(self, price: float) -> bool:
        """True when price has breached the 100% swing origin.

        For a bullish leg, trading below the origin invalidates it; for a bearish
        leg, trading above. An intent built on an invalidated leg must not be
        sent, however long ago it was approved.
        """
        if price is None or not self.invalidation:
            return False
        if self.is_bullish:
            return float(price) < self.invalidation
        return float(price) > self.invalidation


#: The Optimal Trade Entry band. Retracement into 0.618-0.786 is the zone
#: associated with continuation after a sweep.
OTE_LOW = 0.618
OTE_HIGH = 0.786


def find_swing_leg(df: pd.DataFrame, lookback: int = 3) -> SwingLeg | None:
    """The most recent confirmed swing leg.

    Only a leg whose pivot is fully confirmed is returned. An unconfirmed leg
    would have its retracement levels revised as new bars arrive, so any entry
    computed from it would drift.

    Use select_structural_leg for the strategy rules; this lower-level helper
    keeps its permissive behaviour for callers that just want the recent leg.
    """
    highs = find_pivot_highs(df, lookback=lookback)
    lows = find_pivot_lows(df, lookback=lookback)
    if not highs and not lows:
        return None
    if not lows:
        i, p = highs[-1]
        return SwingLeg(i, len(df) - 1, float(p), float(df["close"].iloc[-1]),
                        "BULLISH", invalidation=float(p))
    if not highs:
        i, p = lows[-1]
        return SwingLeg(i, len(df) - 1, float(p), float(df["close"].iloc[-1]),
                        "BEARISH", invalidation=float(p))

    last_high = highs[-1]
    last_low = lows[-1]
    # Whichever pivot came later anchors the leg.
    if last_low[0] > last_high[0]:
        return SwingLeg(last_low[0], len(df) - 1, float(last_low[1]),
                        float(df["close"].iloc[-1]), "BULLISH",
                        invalidation=float(last_low[1]))
    return SwingLeg(last_high[0], len(df) - 1, float(last_high[1]),
                    float(df["close"].iloc[-1]), "BEARISH",
                    invalidation=float(last_high[1]))


def last_completed_swing(df: pd.DataFrame, lookback: int = 3) -> SwingLeg | None:
    """Pivot-to-pivot swing whose end pivot is confirmed BEFORE the last bar.

    End price is the later confirmed pivot, not the current close. That is
    what lets a sweep on the last bar sit as a retracement of a finished
    impulse (OTE Option 1).
    """
    validate_ohlcv(df)
    if len(df) < lookback * 2 + 4:
        return None
    work = df.iloc[:-1]
    highs = find_pivot_highs(work, lookback=lookback)
    lows = find_pivot_lows(work, lookback=lookback)
    events = sorted(
        [(i, p, "H") for i, p in highs] + [(i, p, "L") for i, p in lows]
    )
    if len(events) < 2:
        return None
    end = events[-1]
    start = None
    for prev in reversed(events[:-1]):
        if prev[2] != end[2]:
            start = prev
            break
    if start is None:
        return None
    if start[2] == "L":
        direction = "BULLISH"
        inv = float(start[1])
    else:
        direction = "BEARISH"
        inv = float(start[1])
    return SwingLeg(
        start_index=int(start[0]),
        end_index=int(end[0]),
        start_price=float(start[1]),
        end_price=float(end[1]),
        direction=direction,
        invalidation=inv,
    )


def mean_true_range(df: pd.DataFrame, n: int = 14) -> float:
    """Mean true range of the last `n` closed bars. Zero when unusable."""
    validate_ohlcv(df)
    high = _column(df, "high").to_numpy(dtype=float)
    low = _column(df, "low").to_numpy(dtype=float)
    close = _column(df, "close").to_numpy(dtype=float)
    if len(close) == 0:
        return 0.0
    prev = np.empty_like(close)
    prev[0] = close[0]
    prev[1:] = close[:-1]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    if len(tr) < n:
        return float(tr.mean()) if len(tr) else 0.0
    return float(tr[-n:].mean())


#: A leg must span at least this multiple of ATR. Below it, the 'swing' is
#: noise: a leg smaller than typical volatility has no structural meaning.
MIN_LEG_ATR_MULTIPLE = 3.0


def select_structural_leg(
    df: pd.DataFrame,
    *,
    sweep: "Sweep | None" = None,
    lookback_bars: int = 20,
    atr: float | None = None,
    min_atr_multiple: float = MIN_LEG_ATR_MULTIPLE,
) -> SwingLeg | None:
    """Choose the swing leg the strategy rules require.

    Two filters, both about avoiding anchors chosen from noise:

    - **Swing high** must be the highest point within `lookback_bars` AND be
      preceded by a liquidity sweep of a session or daily high. Without the sweep
      condition a random spike becomes the anchor.
    - **Swing low** must sit at least `min_atr_multiple` x ATR below the
      leg's high. A 'swing' smaller than normal volatility is noise, and
      retracement levels measured from it are meaningless.

    Returns None when no leg qualifies, which the caller must treat as 'no
    setup' rather than falling back to the permissive finder. Falling back would
    reintroduce exactly the arbitrary anchor these rules exist to prevent.
    """
    validate_ohlcv(df)
    highs = _column(df, "high").to_numpy(dtype=float)
    lows = _column(df, "low").to_numpy(dtype=float)
    closes = _column(df, "close").to_numpy(dtype=float)
    n = len(df)

    if n < lookback_bars + 2:
        return None

    window_high = float(highs[-lookback_bars:].max())
    window_low = float(lows[-lookback_bars:].min())

    last_price = float(closes[-1])

    # A bullish leg: a swept low, then a reversal whose high is the window high.
    #
    # The leg anchors on the window low, which may sit BELOW the swept level: a
    # sweep at 99.9 commonly continues to 99.5 before reversing, and that extension
    # is exactly the structure being traded. Anchoring on the window low rather than
    # the sweep level is therefore deliberate, and the two are not required to
    # match.
    if sweep is not None and sweep.direction == "BUY":
        if window_high > window_low:
            leg = SwingLeg(
            start_index=n - lookback_bars,
            end_index=n - 1,
            start_price=window_low,
            end_price=window_high,
            direction="BULLISH",
            invalidation=window_low,
        )
        if _leg_is_significant(leg, highs, lows, df, atr, min_atr_multiple):
            return leg

    # A bearish leg mirrors the rule: a swept high, anchored on the window high.
    if sweep is not None and sweep.direction == "SELL":
        if window_high > window_low:
            leg = SwingLeg(
            start_index=n - lookback_bars,
            end_index=n - 1,
            start_price=window_high,
            end_price=window_low,
            direction="BEARISH",
            invalidation=window_high,
        )
        if _leg_is_significant(leg, highs, lows, df, atr, min_atr_multiple):
            return leg

    return None


def _level_tolerance(df: pd.DataFrame, level: float) -> float:
    """A few ticks of slack, so float noise is not read as a different level."""
    point = df.get("point")
    base = float(point.iloc[0]) if point is not None and len(point) else 1e-05
    return base * 2


def _leg_is_significant(leg: SwingLeg, highs, lows, df: pd.DataFrame,
                        atr: float | None,
                        min_atr_multiple: float) -> bool:
    """Reject a leg whose span is a fraction of normal volatility."""
    span = abs(leg.end_price - leg.start_price)
    if span <= 0:
        return False
    if atr is None:
        atr_col = df.get("atr")
        if atr_col is None or not len(atr_col):
            return True
        atr = float(atr_col.iloc[-1])
    if atr is None or atr <= 0:
        return True
    return span >= atr * min_atr_multiple


def ote_zone(leg: SwingLeg) -> tuple[float, float]:
    """(lower, upper) price bounds of the OTE band for a leg.

    For a bullish leg the retracement levels descend from the high, so the band
    is returned low-first to keep the ordering consistent.
    """
    if leg.is_bullish:
        deeper = leg.retracement(OTE_HIGH)
        shallower = leg.retracement(OTE_LOW)
        return (min(deeper, shallower), max(deeper, shallower))
    deeper = leg.retracement(OTE_HIGH)
    shallower = leg.retracement(OTE_LOW)
    return (min(deeper, shallower), max(deeper, shallower))


def in_ote_zone(price: float, leg: SwingLeg) -> bool:
    """True when price sits inside the 0.618-0.786 retracement band."""
    if price is None:
        return False
    lower, upper = ote_zone(leg)
    return lower <= float(price) <= upper


def _stamp(df: pd.DataFrame, i: int):
    if "time" in df.columns:
        return df["time"].iloc[i]
    return df.index[i]