"""Build the pre-dispatch structural guard.

`LiveGuard` re-checks the 100% swing anchor immediately before an order leaves.
The engine layer must not import `data_engine` -- candles and quotes arrive
through the gateway or an adapter -- so this module builds the guard from
**callables the caller supplies**. Nothing here touches MT5, which is what
makes the guard testable against a hand-built DataFrame and a stub price.

Two callers, one guard:

  * the AI Signals path already holds the leg its intent was built on, so it
    passes it in and the guard tests exactly that structure;
  * the Exchange tab has no leg, so the guard derives the current one from a
    fresh candle snapshot using the same rules the strategy uses.

Both get the same `LiveGuard`, which is the point: a manual order must not be
able to skip a check an automated one cannot.

Fail-closed, per the guard's own contract ("anything it cannot evaluate
blocks"): if the candles are unavailable, or the price cannot be read, the
check raises and `LiveGuard.verify` converts that into a refusal. Absence of a
*leg* is different -- see `_derive_leg`.
"""

from __future__ import annotations

from typing import Any, Callable

from engine.executor import LiveGuard
from engine.indicators.structure import (
    detect_sweeps,
    daily_extremes,
    select_structural_leg,
)

#: Bars pulled when deriving the anchor for a manual order. The structural
#: rules look back 20 bars; the same headroom the dry-run harness uses keeps
#: the derived leg comparable to the one the scan would have produced.
DEFAULT_LOOKBACK_BARS = 20


def _is_empty(bars: Any) -> bool:
    """True for None, an empty frame, or anything without rows.

    Deliberately duck-typed rather than `isinstance(bars, pd.DataFrame)`: the
    guard is handed whatever the caller's candle source returns, and a truthiness
    check on a DataFrame raises, so `len()` is the only safe probe.
    """
    if bars is None:
        return True
    try:
        return len(bars) == 0
    except TypeError:
        return True


def _last_atr(bars: Any) -> float | None:
    """ATR of the most recent bar, or None when the frame has no ATR column.

    The strategy's candle frames carry it; a bare OHLC frame does not, and
    `_leg_is_significant` already treats a missing ATR as 'significant'.
    """
    try:
        column = bars["atr"]
    except (KeyError, TypeError):
        return None
    if column is None or not len(column):
        return None
    try:
        return float(column.iloc[-1])
    except (TypeError, ValueError):
        return None


def _derive_leg(bars: Any, side: str, lookback_bars: int):
    """The structural leg the current snapshot supports for `side`, or None.

    Returns None when the snapshot holds no direction-matched sweep or no leg
    that clears the 3x ATR significance bar. None is a *pass*, not a block: the
    structural rules are strict, so 'no qualifying leg' is the common case
    rather than evidence that something broke. Blocking on it would refuse
    nearly every manual order -- the guard would be rejecting what it could not
    evaluate rather than what it found invalidated, which is a different and
    much weaker rule. Only *unavailability* fails closed.
    """
    sweeps = detect_sweeps(bars, levels=daily_extremes(bars),
                           lookback=lookback_bars)
    wanted = str(side).upper()
    matching = [s for s in sweeps if s.direction == wanted]
    if not matching:
        return None
    return select_structural_leg(bars, sweep=matching[-1],
                                 lookback_bars=lookback_bars,
                                 atr=_last_atr(bars))


def make_live_guard(
    *,
    side: str,
    bars_provider: Callable[[str], Any],
    price_provider: Callable[[str], float | None],
    leg: Any = None,
    lookback_bars: int = DEFAULT_LOOKBACK_BARS,
) -> LiveGuard:
    """Build the guard that re-checks invalidation at the instant of dispatch.

    `bars_provider(symbol)` returns a candle frame (raises/returns empty when
    the feed is down). `price_provider(symbol)` returns the current price, or
    None. `leg` is the anchor the intent was built on, when the caller has one.

    Note that `LiveGuard.price_provider` is never read by `verify` -- the field
    exists but the check is `invalidation_check`, so the price source has to be
    captured by the closure here. That is why this factory exists rather than
    the caller assembling a guard by hand.
    """
    side = str(side).upper()

    def invalidation_check(symbol: str) -> bool:
        """True when the 100% swing anchor is breached. Raises when unverifiable.

        Returning True means "breached"; `LiveGuard.verify` turns that into a
        refusal. Raising means "could not check", which it also turns into a
        refusal -- so both paths fail closed.
        """
        bars = bars_provider(symbol)
        if _is_empty(bars):
            raise RuntimeError(
                f"no candle snapshot for {symbol}; the swing anchor cannot be "
                f"re-checked, so the setup is treated as unverified"
            )

        anchor = leg if leg is not None else _derive_leg(bars, side, lookback_bars)
        if anchor is None:
            # Nothing to invalidate. Not a breach -- see `_derive_leg`.
            return False

        price = price_provider(symbol)
        if price is None:
            raise RuntimeError(
                f"no current price for {symbol}; the swing anchor cannot be "
                f"re-checked, so the setup is treated as unverified"
            )
        return bool(anchor.is_invalidated(float(price)))

    return LiveGuard(invalidation_check=invalidation_check)
