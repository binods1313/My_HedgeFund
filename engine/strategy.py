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
    Sweep,
    SwingLeg,
    daily_extremes,
    find_swing_leg,
    in_ote_zone,
    latest_sweep,
    unfilled_fvgs,
    validate_ohlcv,
)
from engine.indicators.trend import EmaRibbon, ema_ribbon, vwap

BUY = "BUY"
SELL = "SELL"


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

    # -- 3. Fibonacci OTE ------------------------------------------------
    leg = find_swing_leg(df)
    signal.leg = leg
    if leg is None:
        signal.blocks.append(Block("fibonacci OTE", False,
                                  "no confirmed swing leg"))
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
    ribbon_ok = (ribbon.direction == wanted)
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