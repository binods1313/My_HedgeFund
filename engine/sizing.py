"""Broker-constrained lot sizing.

    Lots = risk_budget / ((sl_distance / tick_size) * tick_value_loss)

Two rules that are easy to get wrong and are both enforced here:

  - **Fail closed below the broker minimum.** A raw size of 0.003 cannot be
    traded, and silently rounding it UP to 0.01 would quadruple the risk the
    cap exists to limit. The intent is dropped instead, and the drop is logged.
  - **Normalisation floors to the volume step.** Rounding up to the next step
    breaches the cap at the moment of placement, so the step is always rounded
    DOWN.

`ui.risk_calc.normalize_lots` is reused rather than reimplemented: it is the
same arithmetic the dashboard shows the operator, so the number they see and the
number we send cannot drift apart.

One caveat worth stating plainly: `normalize_lots` ends with
`max(volume_min, ...)`, so it clamps upward to the minimum. The fail-closed
rule therefore cannot live inside it and is applied here, BEFORE normalising.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from engine.risk import RiskConfigError

#: Logged and recorded when the computed size is below the broker minimum.
LOTS_BELOW_MINIMUM_LOG = "LOTS_BELOW_MINIMUM_CAP"

#: Relative slack when comparing a computed size against the broker minimum.
#: Absorbs binary floating-point error so an order sized to land exactly on the
#: minimum is not rejected. 1e-9 of the minimum is orders of magnitude tighter
#: than any real difference in risk.
LOT_EPSILON = 1e-9


@dataclass(frozen=True)
class SizingResult:
    """Outcome of sizing one order."""

    ok: bool
    lots: float
    raw_lots: float
    risk_amount: float
    reason: str = ""
    log_message: str = ""

    @property
    def failed(self) -> bool:
        return not self.ok


class LotSizer:
    """Computes lot size from a risk budget and a stop distance."""

    def __init__(self, spec):
        """`spec` is a ui.risk_calc.SymbolSpec built from live broker data."""
        self.spec = spec

    @property
    def risk_budget(self) -> float:
        return float(self.spec.risk_budget) if hasattr(
            self.spec, "risk_budget") else 0.0

    def size(self, entry: float, stop: float,
             risk_budget: Optional[float] = None) -> SizingResult:
        """Size a position, or fail closed with a reason."""
        budget = risk_budget if risk_budget is not None else 0.0
        distance = abs(float(entry) - float(stop))

        tick_size = float(self.spec.tick_size)
        tick_value = float(self.spec.tick_value_loss)

        if distance <= 0:
            return SizingResult(False, 0.0, 0.0, 0.0,
                                reason="stop distance is zero")
        if tick_size <= 0 or tick_value <= 0:
            return SizingResult(
                False, 0.0, 0.0, 0.0,
                reason="broker tick spec unusable "
                       f"(tick_size={tick_size}, tick_value={tick_value})")

        ticks = distance / tick_size
        risk_per_lot = ticks * tick_value
        if risk_per_lot <= 0:
            return SizingResult(False, 0.0, 0.0, 0.0,
                                reason="computed risk per lot is zero")

        raw_lots = budget / risk_per_lot

        # Fail closed BEFORE normalising: normalize_lots clamps up to the
        # minimum, which would turn an untradeable size into an over-risk one.
        #
        # The comparison carries a relative epsilon. 10 / (1000 * 1.0) evaluates
        # to 0.009999999999999992 in binary floating point, which is below a
        # 0.01 minimum purely by representation error. A fail-closed rule that
        # discards orders sitting exactly on the boundary is wrong in the other
        # direction: it refuses trades the rule permits. The epsilon is far
        # tighter than any real risk difference, so it cannot admit a genuine
        # overspend.
        tolerance = float(self.spec.volume_min) * LOT_EPSILON
        if raw_lots < float(self.spec.volume_min) - tolerance:
            return SizingResult(
                False, 0.0, raw_lots, 0.0,
                reason=f"{LOTS_BELOW_MINIMUM_LOG}: {raw_lots:.5f} lots is below "
                       f"the {self.spec.volume_min} minimum; a "
                       f"{budget:.2f} budget cannot absorb this stop",
                log_message=LOTS_BELOW_MINIMUM_LOG)

        from ui.risk_calc import normalize_lots

        lots = normalize_lots(raw_lots, self.spec)
        if lots <= 0:
            return SizingResult(False, 0.0, raw_lots, 0.0,
                                reason="normalised size is zero",
                                log_message=LOTS_BELOW_MINIMUM_LOG)

        return SizingResult(
            ok=True, lots=lots, raw_lots=raw_lots,
            risk_amount=risk_per_lot * lots)