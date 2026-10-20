"""Confluence assembly: five blocks into one directional signal.

A signal exists only when the blocks agree. Individually they are weak, and a
framework that fires on 'any block present' would produce a signal on most
bars. Requiring agreement is what makes the output worth reviewing.

Alignment rule, and it is directional:

  Liquidity sweep   BUY after price sweeps a LOW;   SELL after it sweeps a HIGH
  Fair value gap    BULLISH gap acts as support;    BEARISH gap as resistance
  Fibonacci OTE     price in the 0.618-0.786 band of the current leg
  EMA ribbon        BULLISH stack and rising slope
  VWAP              price above for a long setup, below for a short one

A bullish setup therefore wants: a low swept, an unfilled bullish gap, price
in the OTE band, a rising EMA stack, and price above VWAP. Anything opposing is
recorded as a conflict and the signal is rejected rather than down-weighted --
silent down-weighting is how a strategy ends up trading setups it contradicts.

This module produces a Signal. It never produces an order. Conversion to an
OrderIntent is `to_intent`, and it requires a tick specification from the
broker, because risk cannot be computed without one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from engine.indicators.structure import (
    MIN_LEG_ATR_MULTIPLE,
    Sweep,
    SwingLeg,
    daily_extremes,
    find_swing_leg,
    in_ote_zone,
    last_completed_swing,
    latest_sweep,
    mean_true_range,
    unfilled_fvgs,
    validate_ohlcv,
)
from engine.indicators.trend import EmaRibbon, ema_ribbon, vwap

BUY = "BUY"
SELL = "SELL"

#: Sweep/setup side is BUY/SELL; the ribbon speaks BULLISH/BEARISH/MIXED.
_SIDE_TO_RIBBON = {BUY: "BULLISH", SELL: "BEARISH"}


def ribbon_matches_side(wanted: str | None, ribbon_direction: str) -> bool:
    """True when the EMA ribbon direction is the same side as the setup.

    BUY maps to BULLISH, SELL to BEARISH. MIXED and the opposite stack
    disagree. Comparing the raw strings (BUY == BULLISH) is always false
    and made five-block agreement unreachable.
    """
    mapped = _SIDE_TO_RIBBON.get(str(wanted or "").upper())
    if mapped is None:
        return False
    return str(ribbon_direction) == mapped


@dataclass(frozen=True)
class BrokerStops:
    """Broker freeze-level inputs for the approved stop floor."""

    point: float
    tick_size: float
    stops_level_points: float
    spread_points: float


def broker_stops_from_view(view: dict | None) -> BrokerStops:
    """Build BrokerStops from a scan snapshot. Spread from ask-bid when present."""
    view = view or {}
    point = float(view.get("point") or view.get("trade_point") or 1e-5)
    if point <= 0:
        point = 1e-5
    tick = float(view.get("tick_size") or view.get("trade_tick_size") or point)
    stops_level = float(view.get("trade_stops_level") or view.get("stops_level") or 0.0)
    spread_pts = float(view.get("spread") or view.get("spread_points") or 0.0)
    if spread_pts <= 0:
        bid = view.get("bid")
        ask = view.get("ask")
        try:
            if bid is not None and ask is not None and float(ask) > float(bid):
                spread_pts = (float(ask) - float(bid)) / point
        except (TypeError, ValueError):
            spread_pts = 0.0
    return BrokerStops(point, tick if tick > 0 else point, stops_level, spread_pts)


def sweep_stop(side: str, entry: float, sweep, atr: float,
               spec: BrokerStops, *, atr_k: float = 1.0
               ) -> tuple[float, float, float] | None:
    """Stop beyond the sweep wick, at least max(atr_k * ATR, broker min).

    Returns (stop, distance, min_dist) or None to reject.
    """
    if sweep is None or side not in (BUY, SELL):
        return None
    if str(getattr(sweep, "direction", "")) != side:
        return None
    if atr is None or atr <= 0 or spec is None or spec.point <= 0:
        return None
    tick = spec.tick_size if spec.tick_size > 0 else spec.point
    if tick <= 0:
        return None
    broker_min = max(0.0, float(spec.stops_level_points) + float(spec.spread_points)) * float(spec.point)
    min_dist = max(float(atr) * float(atr_k), broker_min)
    if min_dist <= 0:
        return None
    entry_f = float(entry)
    level = float(sweep.level)
    extreme = float(sweep.extreme)
    if side == BUY:
        wick = level - extreme
        if wick >= entry_f:
            return None
        beyond = wick - tick
        stop = min(beyond, entry_f - min_dist)
        dist = entry_f - stop
        if stop >= entry_f or dist + 1e-12 < min_dist:
            return None
    else:
        wick = level + extreme
        if wick <= entry_f:
            return None
        beyond = wick + tick
        stop = max(beyond, entry_f + min_dist)
        dist = stop - entry_f
        if stop <= entry_f or dist + 1e-12 < min_dist:
            return None
    return (float(stop), float(dist), float(min_dist))


def attach_trade_levels(signal: "Signal", df: pd.DataFrame, spec: BrokerStops,
                        *, atr_k: float = 1.0) -> "Signal":
    """Set stop_loss and take_profit (2R) or reject_reason=stop_quality."""
    signal.reject_reason = None
    if signal is None or not signal.is_actionable:
        return signal
    atr_value = mean_true_range(df)
    placed = sweep_stop(signal.direction, signal.price, signal.sweep,
                        atr_value, spec, atr_k=atr_k)
    if placed is None:
        signal.reject_reason = "stop_quality"
        signal.stop_loss = None
        signal.take_profit = None
        signal.stop_distance = None
        return signal
    stop, dist, _min_dist = placed
    signal.stop_loss = stop
    signal.stop_distance = dist
    if signal.direction == BUY:
        signal.take_profit = float(signal.price) + 2.0 * dist
    else:
        signal.take_profit = float(signal.price) - 2.0 * dist
    return signal


@dataclass(frozen=True)
class Block:
    """One building block's verdict, with the reasoning retained."""

    name: str
    agrees: bool
    detail: str
    required: bool = True

    def line(self) -> str:
        mark = "AGREE" if self.agrees else "OPPOSE"
        return f"{self.name:<18} {mark:<7} {self.detail}"


@dataclass
class Signal:
    """A directional setup, or the reasons there is none."""

    symbol: str
    direction: Optional[str]
    blocks: list[Block] = field(default_factory=list)
    confidence: float = 0.0
    price: float = 0.0
    sweep: Optional[Sweep] = None
    leg: Optional[SwingLeg] = None
    ribbon: Optional[EmaRibbon] = None
    vwap_value: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    stop_distance: Optional[float] = None
    reject_reason: Optional[str] = None

    @property
    def is_actionable(self) -> bool:
        return self.direction is not None

    @property
    def required_blocks(self) -> list[Block]:
        return [b for b in self.blocks if b.required]

    @property
    def conflicts(self) -> list[Block]:
        return [b for b in self.required_blocks if not b.agrees]

    def report(self) -> str:
        lines = [f"SIGNAL  {self.symbol}  "
                 f"direction={self.direction or 'NONE'}  "
                 f"confidence={self.confidence:.0%}  price={self.price}"]
        lines.append("-" * 78)
        for block in self.blocks:
            lines.append("  " + block.line())
        if self.conflicts:
            lines.append("-" * 78)
            lines.append("  CONFLICTS: " + ", ".join(
                b.name for b in self.conflicts))
        lines.append("-" * 78)
        return "\n".join(lines)


def evaluate(symbol: str, df: pd.DataFrame, *,
             direction: Optional[str] = None,
             require_vwap: bool = True,
             atr: Optional[float] = None,
             levels: Optional[dict] = None,
             lookback: int = 20) -> Signal:
    """Assess all five blocks and return the assembled signal.

    `direction` forces the setup direction, which is what a scanner does after
    it has decided where to look. Left None, the sweep decides it, since a sweep
    is directional in itself.
    """
    validate_ohlcv(df, min_rows=30)
    price = float(df["close"].iloc[-1])

    signal = Signal(symbol=symbol, direction=None, price=price)

    # -- 1. liquidity sweep ---------------------------------------------
    sweep = latest_sweep(df, levels=levels, lookback=lookback)
    signal.sweep = sweep
    sweep_direction = sweep.direction if sweep else None
    if sweep is None:
        signal.blocks.append(Block(
            "liquidity sweep", False,
            "no reclaim of a prior extreme in the lookback window"))
    else:
        signal.blocks.append(Block(
            "liquidity sweep", True,
            f"{sweep.direction} sweep of {sweep.level:g}, "
            f"extended {sweep.extreme:g} and reclaimed"))

    wanted = direction or sweep_direction
    if wanted and sweep is not None and sweep.direction != wanted:
        signal.blocks.append(Block(
            "sweep alignment", False,
            f"sweep is {sweep.direction}, setup direction is {wanted}"))

    # -- 2. fair value gap ----------------------------------------------
    gap_direction = None
    if wanted == BUY:
        gaps = unfilled_fvgs(df, direction="BULLISH", atr=atr)
        gap_direction = "BULLISH"
    elif wanted == SELL:
        gaps = unfilled_fvgs(df, direction="BEARISH", atr=atr)
        gap_direction = "BEARISH"
    else:
        gaps = []

    if gaps:
        gap = gaps[-1]
        signal.blocks.append(Block(
            "fair value gap", True,
            f"unfilled {gap.direction} gap {gap.bottom:g}-{gap.top:g}, "
            f"height {gap.height:g}"))
    else:
        signal.blocks.append(Block(
            "fair value gap", False,
            f"no unfilled {gap_direction or ''} gap".strip()))

    # -- 3. Fibonacci OTE (Option 1: last completed swing before this bar)
    atr_value = atr if atr is not None else mean_true_range(df)
    leg = last_completed_swing(df)
    if leg is not None and atr_value and atr_value > 0:
        span = abs(float(leg.end_price) - float(leg.start_price))
        if span < MIN_LEG_ATR_MULTIPLE * atr_value:
            leg = None
    signal.leg = leg
    if leg is None:
        signal.blocks.append(Block("fibonacci OTE", False,
                                  "no completed swing before the sweep bar"))
        in_zone = False
    else:
        in_zone = in_ote_zone(price, leg)
        from engine.indicators.structure import ote_zone
        lower, upper = ote_zone(leg)
        signal.blocks.append(Block(
            "fibonacci OTE", in_zone,
            f"price {price:g} "
            f"{'inside' if in_zone else 'outside'} {lower:g}-{upper:g}"))

    # -- 4. EMA ribbon ---------------------------------------------------
    ribbon = ema_ribbon(df)
    signal.ribbon = ribbon
    ribbon_ok = ribbon_matches_side(wanted, ribbon.direction)
    signal.blocks.append(Block(
        "ema ribbon", ribbon_ok,
        f"{ribbon.direction} stack "
        f"({ribbon.fast:g}/{ribbon.mid:g}/{ribbon.slow:g}), "
        f"slope {ribbon.mid_slope:+g}"))

    # -- 5. VWAP ---------------------------------------------------------
    if "tick_volume" in df.columns:
        series = vwap(df, anchor="session")
        vwap_value = float(series.iloc[-1]) if series.notna().any() else None
        signal.vwap_value = vwap_value
        if vwap_value is None:
            signal.blocks.append(Block("vwap", False,
                                      "no volume in the current session",
                                      required=require_vwap))
        else:
            vwap_ok = (price > vwap_value) if wanted == BUY else (price < vwap_value)
            signal.blocks.append(Block(
                "vwap", vwap_ok,
                f"price {price:g} "
                f"{'above' if price > vwap_value else 'below'} "
                f"session VWAP {vwap_value:g}"))
    else:
        vwap_value = None
        signal.blocks.append(Block("vwap", False,
                                  "tick_volume absent; VWAP not computable",
                                  required=require_vwap))

    # -- assemble --------------------------------------------------------
    required = signal.required_blocks
    agreed = [b for b in required if b.agrees]
    if required:
        signal.confidence = len(agreed) / len(required)

    # A signal needs every required block to agree. Anything opposing is a
    # conflict, not a discount.
    if not signal.conflicts:
        signal.direction = wanted
    else:
        signal.direction = None

    return signal


def to_intent(signal: Signal, *, symbol: str, volume: float, stop_loss: float,
              tick_value: float, tick_size: float,
              take_profit: Optional[float] = None):
    """Convert an actionable signal into an OrderIntent.

    Refuses a non-actionable signal. Building an intent from a signal with
    conflicts would let the risk layer approve something the strategy itself
    rejected.
    """
    from engine.risk import OrderIntent

    if not signal.is_actionable:
        raise ValueError(
            f"{signal.symbol}: refusing to build an intent from a "
            f"non-actionable signal (conflicts: "
            f"{[b.name for b in signal.conflicts]})"
        )
    return OrderIntent.from_symbol_spec(
        symbol=symbol,
        side=signal.direction,
        volume=volume,
        entry_price=signal.price,
        stop_loss=stop_loss,
        tick_value=tick_value,
        tick_size=tick_size,
    )