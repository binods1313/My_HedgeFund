"""Dry-run harness: the full path up to, but not including, order submission.

This exists to answer one question before any real order is sent: does a signal
built from live candles survive the real risk gate?

The path exercised is the production path, not a rehearsal of it:

    live candles -> indicators -> confluence -> OrderIntent -> RiskApproval
                -> journal

What it deliberately does NOT do: call order_send. The gateway is used for
reads only, and the harness asserts on that by construction -- it holds no
reference to the order-submission entry point at all, and a test proves the
module cannot reach one.

A dry run that placed an order would be an integration test with a side effect,
which is how demo accounts lose money by accident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from engine.executor import RiskApproval
from engine.risk import (
    AccountState,
    OrderIntent,
    RiskEngine,
    RiskLimits,
)
from engine.sizing import LotSizer
from engine.strategy import BUY, Signal, evaluate, to_intent
from engine.validation import (
    MIN_CONFLUENCES_FOR_XAI,
    SpatialContext,
    XAIValidationGate,
)

#: Minimum confluences before an intent is compiled.
MIN_CONFLUENCES = MIN_CONFLUENCES_FOR_XAI

#: Bars fetched for indicator work. 200 is the minimum for a stable EMA200;
#: 300 gives the slow ribbon room to settle.
DEFAULT_BARS = 300

#: Default volume for a dry run, chosen to sit at the broker's minimum so the
#: arithmetic matches what would actually be sent.
DEFAULT_VOLUME = 0.01


@dataclass
class DryRunResult:
    """Everything that happened, so a dry run can be reviewed rather than trusted."""

    symbol: str
    direction: Optional[str]
    ran: bool
    reason: str = ""
    intent: Optional[OrderIntent] = None
    approval: Optional[RiskApproval] = None
    risk_amount: Optional[float] = None
    risk_pct: Optional[float] = None
    max_volume: Optional[float] = None
    account: Optional[AccountState] = None
    signal: Optional[Signal] = None
    bars_used: int = 0
    gate_passed: bool = False
    gate_consulted: bool = False
    gate_reason: str = ""
    chosen_volume: Optional[float] = None

    @property
    def gate_approved(self) -> bool:
        return bool(self.approval and self.approval.approved)

    def report(self) -> str:
        lines = ["=" * 78,
                 f"  DRY RUN  {self.symbol}",
                 "=" * 78]
        if not self.ran:
            lines.append(f"  [NOT RUN] {self.reason}")
            return "\n".join(lines)

        if self.signal is not None:
            lines.append(self.signal.report())
            lines.append("")

        lines.append("  ORDER INTENT")
        if self.intent is None:
            lines.append("    (none built)")
        else:
            lines.append(f"    side      : {self.intent.side}")
            lines.append(f"    volume    : {self.intent.volume}")
            lines.append(f"    entry     : {self.intent.entry_price}")
            lines.append(f"    stop loss : {self.intent.stop_loss}")
            lines.append(f"    risk      : "
                         f"{self.risk_amount:.2f} account currency"
                         if self.risk_amount is not None
                         else "    risk      : (not computed)")

        lines.append("")
        lines.append("  xAI STRUCTURAL GATE")
        if not self.gate_consulted:
            lines.append("    verdict   : not consulted")
        else:
            lines.append(f"    verdict   : "
                         f"{'PASS' if self.gate_passed else 'FAIL'}")
        if self.gate_reason:
            lines.append(f"    reason    : {self.gate_reason}")

        lines.append("")
        lines.append("  RISK GATE")
        if self.approval is None:
            lines.append("    (gate not consulted)")
        else:
            mark = "APPROVED" if self.approval.approved else "REJECTED"
            lines.append(f"    verdict   : {mark}")
            if self.approval.approved:
                lines.append(f"    risk      : {self.risk_amount:.2f} "
                             f"({self.risk_pct:.3f}% of equity)")
                lines.append(f"    volume    : {self.chosen_volume} "
                             f"(cap {self.max_volume})")
            else:
                for reason in self.approval.reasons:
                    lines.append(f"    reason    : {reason}")

        if self.account is not None:
            lines.append("")
            lines.append("  ACCOUNT STATE SUPPLIED TO THE GATE")
            lines.append(f"    equity            : {self.account.equity}")
            lines.append(f"    start_of_day      : "
                         f"{self.account.start_of_day_equity}")
            lines.append(f"    peak              : {self.account.peak_equity}")
            lines.append(f"    open trades       : {self.account.open_trade_count}")
            lines.append(f"    total open risk   : {self.account.total_open_risk}")
            lines.append(f"    unprotected       : "
                         f"{self.account.unprotected_positions}")

        lines.append("")
        lines.append("  NO ORDER WAS SENT. This harness holds no reference to the")
        lines.append("  order-submission entry point, so it cannot place one.")
        return "\n".join(lines)


def _spatial_context(symbol: str, signal: Signal, bars: pd.DataFrame,
                     timeframe: str = "M15") -> SpatialContext:
    """Assemble the six numeric fields the xAI gate is shown.

    Every value is derived from the snapshot, never free text, because the
    hallucination check bounds the model against exactly these numbers.
    """
    from engine.indicators.structure import ote_zone

    fvg_size = 0.0
    for block in signal.blocks:
        if block.name == "fair value gap" and "height" in block.detail:
            # detail reads: 'unfilled BULLISH gap 1.12318-1.12383, height 0.00065'
            for part in block.detail.split(","):
                if part.strip().startswith("height"):
                    try:
                        fvg_size = abs(float(part.split()[1]))
                    except (IndexError, ValueError):
                        fvg_size = 0.0

    ote_distance = 0.0
    if signal.leg is not None:
        lower, upper = ote_zone(signal.leg)
        midpoint = (lower + upper) / 2
        ote_distance = abs(signal.price - midpoint)

    trend_bias = signal.ribbon.direction if signal.ribbon else "MIXED"
    sweep_type = signal.sweep.direction if signal.sweep else "NONE"

    return SpatialContext(
        symbol=symbol,
        timeframe=timeframe,
        fvg_size=fvg_size,
        sweep_type=sweep_type,
        ote_distance=ote_distance,
        trend_bias=trend_bias,
    )


def max_volume_for_risk(entry: float, stop: float, tick_value: float,
                        tick_size: float, risk_budget: float,
                        floor: float = 0.01) -> float:
    """Largest volume whose stop-loss risk fits the budget.

    Floored at the broker minimum and rounded DOWN to two decimals, because
    rounding up would breach the budget the moment it is placed.
    """
    distance = abs(entry - stop)
    if distance <= 0 or tick_size <= 0 or tick_value <= 0:
        return floor
    per_lot = distance / tick_size * tick_value
    if per_lot <= 0:
        return floor
    import math
    return max(floor, math.floor((risk_budget / per_lot) * 100) / 100)


def run_dry_run(symbol: str, bars: pd.DataFrame, *,
                equity: float,
                start_of_day_equity: Optional[float] = None,
                peak_equity: Optional[float] = None,
                total_open_risk: Optional[float] = None,
                open_trade_count: int = 0,
                unprotected_positions: int = 0,
                tick_value: Optional[float] = None,
                tick_size: Optional[float] = None,
                volume: float = DEFAULT_VOLUME,
                stop_distance_pips: Optional[float] = None,
    limits: Optional[RiskLimits] = None,
    risk: Optional[RiskEngine] = None,
    specs: Optional[dict] = None,
    direction: Optional[str] = None,
    require_vwap: bool = True,
    xai_gate: Optional[XAIValidationGate] = None,
    sizer: Optional[LotSizer] = None) -> DryRunResult:
    """Run the full pre-submission path against supplied data.

    Every input is explicit. Nothing is read from the environment or the clock,
    so a dry run is reproducible from its inputs alone.

    The order is deliberate and matches the live path:

        indicators -> 3-of-5 threshold -> xAI gate -> sizing -> risk gate

    xAI sits before sizing and before risk, so a rejected setup never reaches
    either. It can only remove a setup; it cannot add one and cannot repair one.
    """
    limits = limits or RiskLimits()
    xai_gate = xai_gate if xai_gate is not None else XAIValidationGate()

    if bars is None or bars.empty:
        return DryRunResult(symbol=symbol, direction=None, ran=False,
                            reason="no candle data")

    # -- indicator and confluence ---------------------------------------
    atr_value = float(bars["atr"].iloc[-1]) if "atr" in bars.columns else None
    signal = evaluate(symbol, bars, direction=direction,
                      require_vwap=require_vwap, atr=atr_value)
    spatial_context = _spatial_context(symbol, signal, bars)

    # -- confluence -----------------------------------------------------
    # The threshold is 3 of 5, not unanimous. Unanimity would mean xAI only
    # ever saw setups the indicators had already accepted, and the model could
    # not act as the structural check it is meant to be.
    required = len(signal.required_blocks)
    agreed = sum(1 for b in signal.required_blocks if b.agrees)
    confluences = agreed

    # -- tick specification ---------------------------------------------
    if tick_value is None or tick_size is None:
        if not specs or symbol.upper() not in {k.upper() for k in specs}:
            return DryRunResult(
                symbol=symbol, direction=signal.direction, ran=False,
                reason="no trade specification for this symbol; risk cannot be "
                       "computed without one",
                signal=signal, bars_used=len(bars),
                gate_consulted=False,
                gate_reason="not reached: no trade specification")
        spec = next(v for k, v in specs.items() if k.upper() == symbol.upper())
        tick_value = float(spec.get("trade_tick_value") or 0.0)
        tick_size = float(spec.get("trade_tick_size") or 0.0)

    if not tick_value or not tick_size:
        return DryRunResult(
            symbol=symbol, direction=signal.direction, ran=False,
            reason=f"tick spec unusable (value={tick_value}, size={tick_size})",
            signal=signal, bars_used=len(bars),
            gate_consulted=False,
            gate_reason="not reached: tick spec unusable")

    # -- account state ---------------------------------------------------
    account = AccountState(
        equity=equity,
        start_of_day_equity=(start_of_day_equity if start_of_day_equity
                              is not None else equity),
        peak_equity=(peak_equity if peak_equity is not None else equity),
        open_trade_count=open_trade_count,
        total_open_risk=(total_open_risk if total_open_risk is not None else 0.0),
        unprotected_positions=unprotected_positions,
    )

    # -- below threshold: stop before the model is consulted ---------------
    if confluences < MIN_CONFLUENCES:
        conflicts = [b.name for b in signal.conflicts]
        return DryRunResult(
            symbol=symbol, direction=None, ran=True,
            reason=(f"{confluences} of {required} confluences is below the "
                    f"{MIN_CONFLUENCES} required"
                    + (f"; opposed: {', '.join(conflicts)}" if conflicts else "")),
            signal=signal, account=account, bars_used=len(bars),
            gate_consulted=False,
            gate_reason=(f"not consulted; only {confluences}/{required} "
                         f"confluences"))

    # -- xAI structural gate ---------------------------------------------
    gate_result = xai_gate.gate(spatial_context, confluences)
    if not gate_result.may_proceed:
        return DryRunResult(
            symbol=symbol, direction=signal.direction, ran=True,
            reason=gate_result.reason,
            signal=signal, account=account, bars_used=len(bars),
            gate_passed=False, gate_consulted=gate_result.consulted,
            gate_reason=gate_result.reason)

    # -- sizing ----------------------------------------------------------
    # Lot size comes from the broker's own tick telemetry, floored to the volume
    # step, and fails closed below the minimum rather than rounding up.
    entry = signal.price
    if stop_distance_pips:
        pip = entry * 1e-4
        stop = round(entry - stop_distance_pips * pip, 5) \
            if signal.direction == BUY else round(entry + stop_distance_pips * pip, 5)
    else:
        stop = round(entry * 0.99, 5) if signal.direction == BUY \
            else round(entry * 1.01, 5)

    if sizer is not None:
        sizing = sizer.size(entry=entry, stop=stop,
                             risk_budget=equity * limits.risk_per_trade)
        if sizing.failed:
            return DryRunResult(
                symbol=symbol, direction=signal.direction, ran=True,
                reason=sizing.reason,
                signal=signal, account=account, bars_used=len(bars),
                gate_passed=True, gate_consulted=True,
                gate_reason=gate_result.reason)
        chosen_lots = sizing.lots
    else:
        risk_budget = equity * limits.risk_per_trade
        max_volume = max_volume_for_risk(entry, stop, tick_value, tick_size,
                                         risk_budget)
        chosen_lots = min(volume, max_volume)

    max_volume = getattr(sizer, "spec", None) and max_volume_for_risk(
        entry, stop, tick_value, tick_size, equity * limits.risk_per_trade)

    try:
        intent = to_intent(
            signal,
            symbol=symbol,
            volume=chosen_lots,
            stop_loss=stop,
            tick_value=tick_value,
            tick_size=tick_size,
        )
    except Exception as exc:   # noqa: BLE001 - reported, not raised
        return DryRunResult(
            symbol=symbol, direction=signal.direction, ran=False,
            reason=f"could not build an intent: {exc}",
            signal=signal, account=account, bars_used=len(bars))
    engine = risk or RiskEngine(limits=limits)
    engine.set_symbol_specs(specs or {symbol: {
        "trade_tick_value": tick_value, "trade_tick_size": tick_size}})
    decision = engine.evaluate(intent, account)
    approval = RiskApproval.from_decision(decision, symbol)

    return DryRunResult(
        symbol=symbol,
        direction=signal.direction,
        ran=True,
        reason="" if approval.approved else "; ".join(decision.violations),
        intent=intent,
        approval=approval,
        risk_amount=intent.risk_amount,
        risk_pct=decision.risk_pct,
        max_volume=max_volume,
        chosen_volume=intent.volume,
        account=account,
        signal=signal,
        bars_used=len(bars),
        gate_passed=True,
        gate_consulted=True,
        gate_reason=gate_result.reason,
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for `python -m engine.dryrun`.

    Without this the module imported cleanly and exited 0 having done nothing,
    which reads as 'dry run passed' when no check had run at all.
    """
    from engine.dryrun_cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    import sys

    sys.exit(main())