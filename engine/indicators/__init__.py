"""Technical indicators.

Pure functions over OHLCV data. No MT5, no clock, no network. Every rule is
testable against a hand-built frame, which is why the signal logic can be
reviewed without a terminal.

    structure.py   liquidity sweeps, fair value gaps, Fibonacci OTE
    trend.py       EMA ribbon, session VWAP

None of these predicts price. They describe where price has been and what the
macro backdrop is. Whether that has edge is a backtesting question, answered
in Phase 5, not here.
"""

from engine.indicators.structure import (
    OTE_HIGH,
    OTE_LOW,
    FairValueGap,
    Sweep,
    SwingLeg,
    daily_extremes,
    detect_sweeps,
    find_fair_value_gaps,
    find_pivot_highs,
    find_pivot_lows,
    find_swing_leg,
    last_completed_swing,
    mean_true_range,
    in_ote_zone,
    latest_sweep,
    ote_zone,
    unfilled_fvgs,
    validate_ohlcv,
)
from engine.indicators.trend import (
    SESSION_WINDOWS,
    EmaRibbon,
    ema_ribbon,
    session_vwap,
    typical_price,
    vwap,
)

__all__ = [
    "OTE_HIGH", "OTE_LOW",
    "FairValueGap", "Sweep", "SwingLeg", "EmaRibbon", "SESSION_WINDOWS",
    "daily_extremes", "detect_sweeps", "ema_ribbon", "find_fair_value_gaps",
    "find_pivot_highs", "find_pivot_lows", "find_swing_leg",
    "last_completed_swing", "mean_true_range", "in_ote_zone",
    "latest_sweep", "ote_zone", "session_vwap", "typical_price",
    "unfilled_fvgs", "validate_ohlcv", "vwap",
]