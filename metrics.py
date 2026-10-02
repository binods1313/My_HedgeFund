"""Pure display computations.

Every value the UI renders is computed here, as a pure function over plain data.
The rule this module exists to enforce: no presentation logic lives in app.py.

That rule is not stylistic. A loss was displayed as a gain because app.py
summed abs(profit) inline, and 343 tests passed because none of them exercised
that expression. The fix was to make the computation importable and testable,
not to add more assertions about the source text.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from formatting import format_money, format_open_risk


def _finite(value, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    f = float(value)
    if math.isnan(f) or math.isinf(f):
        return default
    return f


@dataclass(frozen=True)
class PositionRisk:
    """Risk for a single open position.

    Risk is the loss incurred if price travels from entry to the stop, i.e.
    the money actually at stake and protected. It is NOT profit and loss:
    an open position showing +500 P&L can still carry 300 of risk.
    """

    ticket: int
    symbol: str
    volume: float
    entry_price: float
    sl: float
    tp: float | None
    risk_amount: float          # account currency at risk, 0.0 if unprotected
    protected: bool

    @property
    def status(self) -> str:
        return "PROTECTED" if self.protected else "UNPROTECTED"


@dataclass(frozen=True)
class OpenRiskSummary:
    """Aggregate risk across a book.

    total_risk_amount is only meaningful for positions that carry a stop. An
    unprotected position has no defined worst case, so its risk is reported as
    unknown rather than as zero; counting it as zero would understate exposure
    and is the failure mode this dataclass is designed to prevent.
    """

    positions: tuple[PositionRisk, ...] = ()
    total_risk_amount: float = 0.0
    equity: float = 0.0
    currency: str | None = None
    unprotected: tuple[PositionRisk, ...] = field(default_factory=tuple)

    @property
    def risk_pct_of_equity(self) -> float | None:
        """Protected risk as a percentage of equity.

        None when equity is unusable, so the UI can print n/a rather than a
        fabricated percentage.
        """
        eq = _finite(self.equity)
        if eq <= 0:
            return None
        return self.total_risk_amount / eq * 100.0

    @property
    def unprotected_count(self) -> int:
        return len(self.unprotected)

    @property
    def has_unprotected(self) -> bool:
        return bool(self.unprotected)

    @property
    def risk_is_partial(self) -> bool:
        """True when some risk could not be quantified at all."""
        return self.has_unprotected

    def risk_pct_display(self) -> str:
        """Human-readable risk percentage, or an explicit unknown marker."""
        pct = self.risk_pct_of_equity
        if pct is None:
            return "N/A"
        return f"{pct:.2f}%"

    def total_risk_display(self) -> str:
        return format_money(self.total_risk_amount, self.currency)


def _tick_value_and_size(info) -> tuple[float, float]:
    """(tick_value, tick_size) in account currency and price units.

    MT5 exposes these on the symbol info. When they are missing we fall back to
    a position of 1.0, which makes risk unavailable rather than invented.
    """
    tv = info.get("trade_tick_value") if isinstance(info, dict) else getattr(
        info, "trade_tick_value", None)
    ts = info.get("trade_tick_size") if isinstance(info, dict) else getattr(
        info, "trade_tick_size", None)
    tv = _finite(tv, 0.0)
    ts = _finite(ts, 0.0)
    if tv <= 0 or ts <= 0:
        return (0.0, 0.0)
    return (tv, ts)


def compute_open_risk(
    positions: Iterable[dict],
    symbol_infos: dict,
    equity: float,
    currency: str | None = None,
) -> OpenRiskSummary:
    """Quantify risk for each open position and aggregate it.

    For a protected position:
        risk = |entry_price - sl| / tick_size * tick_value * volume

    A position with no stop loss (sl falsy) has no defined worst case. It is
    reported as UNPROTECTED and excluded from the total, with
    `risk_is_partial` set so the caller can warn that the figure is incomplete.

    Args:
        positions:    dicts with ticket, symbol, volume, price_open, sl, tp.
        symbol_infos: symbol -> info exposing trade_tick_value/tick_size,
                      either as a mapping or an object with those attributes.
        equity:       account equity, used for the percentage denominator.
        currency:     account currency, for formatting only.
    """
    rows: list[PositionRisk] = []
    unprotected: list[PositionRisk] = []

    for pos in positions or []:
        ticket = int(_finite(pos.get("ticket"), 0))
        symbol = str(pos.get("symbol", ""))
        volume = _finite(pos.get("volume"), 0.0)
        entry = _finite(pos.get("price_open"), 0.0)
        sl = _finite(pos.get("sl"), 0.0)
        tp = pos.get("tp")
        tp = _finite(tp) if tp else None

        info = (symbol_infos or {}).get(symbol)
        has_sl = sl > 0
        risk_amount = 0.0
        protected = False

        if has_sl and info is not None and volume > 0:
            tick_value, tick_size = _tick_value_and_size(info)
            if tick_value > 0 and tick_size > 0:
                ticks = abs(entry - sl) / tick_size
                risk_amount = ticks * tick_value * volume
                protected = True

        row = PositionRisk(
            ticket=ticket,
            symbol=symbol,
            volume=volume,
            entry_price=entry,
            sl=sl,
            tp=tp,
            risk_amount=risk_amount,
            protected=protected,
        )
        rows.append(row)
        if not protected:
            unprotected.append(row)

    total = sum(r.risk_amount for r in rows if r.protected)

    return OpenRiskSummary(
        positions=tuple(rows),
        total_risk_amount=total,
        equity=_finite(equity),
        currency=currency,
        unprotected=tuple(unprotected),
    )


def format_risk_colour(summary: OpenRiskSummary) -> str:
    """Hex colour for the risk figure.

    Amber when any position is unprotected: the number is real but incomplete,
    and showing it in calm green would understate the danger.
    """
    if summary.has_unprotected:
        return "#F59E0B"   # amber
    pct = summary.risk_pct_of_equity
    if pct is None:
        return "#94A3B8"   # slate: no equity to compare against
    if pct >= 5.0:
        return "#F43F5E"   # red
    if pct >= 2.0:
        return "#FBBF24"   # amber
    return "#34D399"       # green


def net_open_pnl(positions: Sequence[dict]) -> float:
    """Signed sum of open position P&L.

    Signed on purpose. Summing magnitudes turns a book sitting on a loss into
    what looks like a gain.
    """
    return sum(_finite(p.get("profit"), 0.0) for p in positions or [])


def format_net_open_pnl(positions: Sequence[dict], currency: str | None) -> str:
    return format_open_risk(net_open_pnl(positions), currency)


def net_open_pnl_colour(positions: Sequence[dict]) -> str:
    """Red on a loss, green on a profit. Keyed off the signed total."""
    return "#F43F5E" if net_open_pnl(positions) < 0 else "#34D399"


def gross_exposure(positions: Sequence[dict]) -> float:
    """Total absolute P&L across open positions: the magnitude, sign discarded.

    This is a magnitude, not P&L. It belongs to at-risk reporting; showing it
    beside a P&L figure invites reading one as the other.
    """
    return sum(abs(_finite(p.get("profit"), 0.0)) for p in positions or [])


# ─────────────────────────────────────────────────────────────────────────────
# LLM confidence presentation
# ─────────────────────────────────────────────────────────────────────────────

# Rendered instead of a number when no score exists. A fail-safe HOLD produces
# no confidence at all; printing a number for it invents a measurement.
NO_SCORE = "n/a"


def format_llm_confidence(confidence, decision: str | None = None) -> str:
    """Render the legacy LLM-asserted confidence.

    Returns NO_SCORE when the model produced no usable score, which includes
    every safety HOLD: a refusal to act carries no confidence in acting.

    The value is the model's own assertion. It is display-only and never gates
    execution; the executable rule is apply_llm_adjustment().
    """
    if decision and str(decision).upper() == "HOLD":
        return NO_SCORE
    if confidence is None:
        return NO_SCORE
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return NO_SCORE
    value = _finite(confidence, default=float("nan"))
    if math.isnan(value) or math.isinf(value):
        return NO_SCORE
    return f"{int(value)}%"


def has_confidence_score(confidence) -> bool:
    """True when a real numeric confidence exists."""
    if confidence is None or isinstance(confidence, bool):
        return False
    if not isinstance(confidence, (int, float)):
        return False
    return not (math.isnan(_finite(confidence, float("nan")))
                or math.isinf(_finite(confidence, float("nan"))))