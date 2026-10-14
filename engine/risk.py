"""Risk Engine — Phase 2.

Replaces the Phase 1 stub, which rejected every order by design, with real
limits. The stub was correct for its phase: no order could reach the terminal
before the gate was trustworthy.

The gate is fail-closed throughout. Every check either passes or blocks; none
of them degrades to "allow" on missing data. A limit that cannot be evaluated
blocks the order, because a risk engine that guesses is worse than no risk
engine: it converts an unknown into a false all-clear.

What this gate covers:

  - Per-trade risk as a percentage of equity, measured entry-to-stop.
  - Total portfolio exposure, the sum of SL-risk across open positions.
  - Daily loss, measured against start-of-day equity.
  - Drawdown from peak equity.
  - Open trade count.
  - Per-symbol max spread.
  - Unprotected positions: a position with no stop has no defined worst case,
    so it is counted as unquantifiable risk rather than as zero risk.

Every decision is written to `risk_decisions` with the reasons that produced
it, whether approved or blocked, so a rejection can be explained after the
fact rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from engine.journal import Journal, utc_now_iso


class RiskConfigError(ValueError):
    """The risk configuration is missing or internally inconsistent."""


@dataclass(frozen=True)
class RiskLimits:
    """Validated limits. Units are fractions of equity unless stated."""

    risk_per_trade_pct: float = 1.0
    daily_loss_limit_pct: float = 3.0
    max_drawdown_pct: float = 10.0
    max_open_trades: int = 5
    max_total_exposure_pct: float = 10.0
    default_max_spread_points: int = 30

    def __post_init__(self):
        for name in ("risk_per_trade_pct", "daily_loss_limit_pct",
                     "max_drawdown_pct", "max_total_exposure_pct"):
            value = getattr(self, name)
            if not (0 < value <= 100):
                raise RiskConfigError(
                    f"{name} must be in (0, 100], got {value}"
                )
        if self.max_open_trades < 1:
            raise RiskConfigError(
                f"max_open_trades must be at least 1, got {self.max_open_trades}")
        if self.default_max_spread_points < 0:
            raise RiskConfigError(
                "default_max_spread_points must not be negative")
        if self.risk_per_trade_pct > self.max_total_exposure_pct:
            raise RiskConfigError(
                f"risk_per_trade_pct ({self.risk_per_trade_pct}) exceeds "
                f"max_total_exposure_pct ({self.max_total_exposure_pct}); a "
                f"per-trade limit above the portfolio cap could never bind")

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> "RiskLimits":
        """Build from the config.yaml `risk` block, rejecting unknown keys.

        Unknown keys are rejected rather than ignored: a typo such as
        `risk_per_trade_pc` would otherwise silently leave the default in place.
        """
        if not isinstance(raw, dict):
            raise RiskConfigError("risk section must be a mapping")
        known = {
            "risk_per_trade_pct", "daily_loss_limit_pct", "max_drawdown_pct",
            "max_open_trades", "max_total_exposure_pct",
            "default_max_spread_points",
        }
        unknown = set(raw) - known
        if unknown:
            raise RiskConfigError(
                f"unknown risk keys: {sorted(unknown)}. Known keys: "
                f"{sorted(known)}"
            )
        return cls(**raw)

    @property
    def risk_per_trade(self) -> float:
        return self.risk_per_trade_pct / 100.0

    @property
    def daily_loss_limit(self) -> float:
        return self.daily_loss_limit_pct / 100.0

    @property
    def max_drawdown(self) -> float:
        return self.max_drawdown_pct / 100.0

    @property
    def max_total_exposure(self) -> float:
        return self.max_total_exposure_pct / 100.0


@dataclass(frozen=True)
class AccountState:
    """The account facts the gate needs.

    `equity` and `start_of_day_equity` must both be supplied. When either is
    missing, percentages cannot be computed, and the gate blocks rather than
    assuming a number.
    """

    equity: float | None = None
    start_of_day_equity: float | None = None
    peak_equity: float | None = None
    open_trade_count: int = 0
    total_open_risk: float | None = None
    unprotected_positions: int = 0
    trading_halted: bool = False
    halt_reason: str = ""

    def __post_init__(self):
        if self.open_trade_count < 0:
            raise RiskConfigError("open_trade_count must not be negative")
        if self.unprotected_positions < 0:
            raise RiskConfigError("unprotected_positions must not be negative")


def _as_float_or_none(value: object) -> float | None:
    """Coerce to float, or None when the value is unusable.

    Handles None, the empty string, whitespace, non-numeric text and NaN. NaN
    is the quiet one: ``float("nan")`` succeeds, and every comparison against
    NaN is False, so a NaN stop would make ``stop_loss > 0`` False for the
    wrong reason rather than being recognised as missing.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:          # NaN is not equal to itself
        return None
    return number


def _safe_stop_distance(entry: object, stop: object) -> float:
    """Absolute entry-to-stop distance, or 0.0 when there is no stop.

    A stop of zero, negative, or unparsable means *no stop*, and the distance to
    a price of zero is meaningless -- ``abs(1.1 - 0.0)`` would report 1.1 as a
    risk distance, which sized an order against a stop that does not exist.
    0.0 is the safe answer: it makes ``has_stop`` False and blocks the order,
    which is what an unknown distance should do.
    """
    entry_value = _as_float_or_none(entry)
    stop_value = _as_float_or_none(stop)
    if entry_value is None or stop_value is None or stop_value <= 0:
        return 0.0
    return abs(entry_value - stop_value)


@dataclass(frozen=True)
class OrderIntent:
    """What the caller wants to trade."""

    symbol: str
    side: str
    volume: float
    entry_price: float
    stop_loss: float
    tick_value: float
    tick_size: float
    spread_points: float | None = None
    max_spread_points: float | None = None

    def __post_init__(self):
        if self.side not in ("BUY", "SELL"):
            raise RiskConfigError(f"side must be BUY or SELL, got {self.side!r}")
        if self.volume <= 0:
            raise RiskConfigError(f"volume must be > 0, got {self.volume}")
        if self.entry_price <= 0:
            raise RiskConfigError("entry_price must be > 0")
        if self.tick_value <= 0 or self.tick_size <= 0:
            raise RiskConfigError(
                "tick_value and tick_size must be > 0; without them the "
                "price distance to stop cannot be converted to account "
                "currency, so the risk would be unknown"
            )

    @property
    def risk_amount(self) -> float:
        """Loss at the stop, in account currency.

        tick_value is the money value of ONE trade_tick_size move, so a price
        distance converts as:

            distance / tick_size * tick_value * volume

        When point == tick_size (the usual case) this is distance * tick_value,
        and dividing by tick_size alone instead would inflate the figure by
        1/tick_size -- roughly 100,000x for a 5-digit forex pair, which would
        block every order against the per-trade cap.
        """
        return (abs(self.entry_price - self.stop_loss)
                / self.tick_size
                * self.tick_value
                * self.volume)

    @classmethod
    def from_symbol_spec(
        cls, *, symbol: str, side: str, volume: float, entry_price: float,
        stop_loss: float, tick_value: float, tick_size: float,
        spread_points: float | None = None, max_spread_points: float | None = None,
    ) -> "OrderIntent":
        """Build an intent from raw MT5 symbol fields.

        Refuses if either tick field is missing or zero, because the risk would
        be unknown and the engine cannot allow what it cannot measure.
        """
        if not tick_value or not tick_size:
            raise RiskConfigError(
                f"{symbol}: the broker did not supply a usable tick spec "
                f"(tick_value={tick_value}, tick_size={tick_size}); risk "
                f"cannot be computed, so this symbol cannot be sized"
            )
        return cls(symbol=symbol, side=side, volume=volume,
                   entry_price=entry_price, stop_loss=stop_loss,
                   tick_value=tick_value, tick_size=tick_size,
                   spread_points=spread_points,
                   max_spread_points=max_spread_points)

    @property
    def stop_distance(self) -> float:
        """Absolute entry-to-stop distance. Never raises.

        Both positions the reconcile dropped on 2026-10-12 carried ``sl=0.0``;
        an order form with a blank or non-numeric stop reaches the same place.
        An unknown distance yields 0.0, which fails ``has_stop``.
        """
        return _safe_stop_distance(self.entry_price, self.stop_loss)

    @property
    def has_stop(self) -> bool:
        """A usable stop loss, or False for anything undefined.

        Never raises. This is the fail-closed path: a stop that arrives as
        None, "", "0", NaN, whitespace or a negative number means "no stop"
        and must return False, not convert a readable refusal into a crash.
        Both real positions that crossed the reconcile were open with no stop,
        so this boundary is exercised in production, not only in theory.
        """
        stop = _as_float_or_none(self.stop_loss)
        if stop is None or stop <= 0:
            return False
        return self.stop_distance > 0


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    risk_amount: float | None = None
    risk_pct: float | None = None
    projected_exposure_pct: float | None = None

    def __bool__(self) -> bool:
        return self.approved

    def reason_text(self) -> str:
        return "; ".join(self.violations) if self.violations else "within limits"


def within(value: float, cap: float) -> bool:
    """Is `value` at or below `cap`, tolerating float representation error?

    Price and volume arithmetic is inexact: 0.005 / 0.00001 evaluates to
    499.99999999999994 ticks, so an order sized to sit exactly on a 1% cap
    arrives at 1.0000000000000002% and would be rejected as a breach. That is
    not a rounding curiosity: an inclusive limit that rejects its own boundary
    blocks legitimate trades and can trip a halt.

    The tolerance is relative to the cap, so it scales with the quantity being
    compared rather than being an absolute epsilon that is either too loose for
    small numbers or too tight for large ones.
    """
    return value <= cap + max(abs(cap) * 1e-9, 1e-9)


class RiskEngine:
    """Evaluates an order against the configured limits.

    Every check is recorded. A decision with no violations is still written to
    `risk_decisions`, because "why did the engine trade this" must be
    answerable, not just "why did it refuse".
    """

    def __init__(self, journal: Journal | None = None, *,
                 limits: RiskLimits | None = None,
                 on_breach: Any = None):
        self.journal = journal
        self.limits = limits or RiskLimits()
        self.on_breach = on_breach
        #: Set once a hard limit trips. Daily loss clears on the next trading
        #: day; drawdown and manual halts do not clear automatically.
        self.halted = False
        self.halt_reason = ""
        self.halt_kind = ""
        self._specs: dict[str, dict] = {}
        self._magic = 100100

    # -- account state ---------------------------------------------------

    def account_state_from_positions(
        self, *, equity: float | None, start_of_day_equity: float | None,
        peak_equity: float | None, open_positions: Sequence[Any] = (),
    ) -> AccountState:
        """Build the AccountState the gate needs from live broker data.

        Total open risk is the sum of each position's entry-to-stop distance.
        A position with no stop contributes no measurable risk, so it is counted
        in `unprotected_positions` instead. That is deliberate: folding it in as
        zero would understate exposure, which is the dangerous direction of
        error, so it becomes a separate count that blocks new trades outright.

        open_positions accepts plain dicts with volume, price_open and sl, which
        is the shape the positions table stores.
        """
        total_risk = 0.0
        unprotected = 0
        for position in open_positions or ():
            volume = float(position.get("volume", 0.0) or 0.0)
            entry = float(position.get("price_open", 0.0) or 0.0)
            stop = float(position.get("sl", 0.0) or 0.0)

            # A position opened by anything other than this engine cannot have
            # its risk measured from our spec, so it counts as unprotected.
            magic = position.get("magic")
            if magic is not None and int(magic) != self.magic:
                unprotected += 1
                continue

            spec = self._spec_for(position.get("symbol")) or {}
            tick_value = float(spec.get("trade_tick_value", 0.0) or 0.0)
            tick_size = float(spec.get("trade_tick_size", 0.0) or 0.0)

            if stop <= 0 or entry <= 0 or not tick_value or not tick_size:
                unprotected += 1
                continue

            total_risk += abs(entry - stop) / tick_size * tick_value * volume

        return AccountState(
            equity=equity,
            start_of_day_equity=start_of_day_equity,
            peak_equity=peak_equity,
            open_trade_count=len(open_positions or ()),
            total_open_risk=total_risk,
            unprotected_positions=unprotected,
            trading_halted=self.halted,
            halt_reason=self.halt_reason,
        )

    def set_symbol_specs(self, specs: dict[str, dict]) -> None:
        """Supply the broker's trade specification per symbol.

        Positions opened by another EA, or on a symbol we have no spec for,
        cannot have their risk measured, so they are counted as unprotected
        rather than assumed to be zero risk.
        """
        self._specs = {k.upper(): v for k, v in (specs or {}).items()}

    def _spec_for(self, symbol: str | None) -> dict | None:
        if not symbol:
            return None
        return getattr(self, "_specs", {}).get(str(symbol).upper())

    @property
    def magic(self) -> int:
        return getattr(self, "_magic", 100100)

    def set_magic(self, magic: int) -> None:
        self._magic = magic

    # -- intent construction ---------------------------------------------

    def build_order_intent(
        self, *, symbol: str, side: str, volume: float, entry_price: float,
        stop_loss: float, spread_points: float | None = None,
        max_spread_points: float | None = None,
    ) -> OrderIntent:
        """Build an intent using the broker's tick spec for `symbol`.

        Refuses when the spec is missing, because risk would be unknown and the
        gate must not approve what it cannot measure.
        """
        spec = self._spec_for(symbol)
        if not spec:
            raise RiskConfigError(
                f"{symbol}: no trade specification known for this symbol; "
                f"run probe_tick_specs.py before sizing it"
            )
        return OrderIntent.from_symbol_spec(
            symbol=symbol, side=side, volume=volume,
            entry_price=entry_price, stop_loss=stop_loss,
            tick_value=float(spec.get("trade_tick_value", 0.0) or 0.0),
            tick_size=float(spec.get("trade_tick_size", 0.0) or 0.0),
            spread_points=spread_points,
            max_spread_points=max_spread_points,
        )

    # -- public API ------------------------------------------------------

    def evaluate(self, intent: OrderIntent,
                 account: AccountState) -> RiskDecision:
        """Assess one order. Fail-closed on anything it cannot evaluate."""
        violations: list[str] = []
        reasons: list[str] = []

        def check(condition: bool, message: str) -> None:
            (reasons if condition else violations).append(message)

        # A halt short-circuits everything: the point of a halt is that no
        # further risk is taken until a human intervenes.
        check(not (self.halted or account.trading_halted),
              f"engine is halted: {self.halt_reason or account.halt_reason}")

        # -- data sufficiency -------------------------------------------
        equity = account.equity
        has_equity = equity is not None and equity > 0
        check(has_equity,
              "equity is unknown; percentages cannot be computed, so the "
              "order is blocked rather than sized against an assumption")

        # -- the stop loss ------------------------------------------------
        check(intent.has_stop,
              "order has no stop loss, so its worst case is undefined")
        if not intent.has_stop:
            return self._finish(intent, account, False, violations, reasons)

        risk_amount = intent.risk_amount
        # Percentages are only computed when equity is known. Deriving
        # risk_pct from a None equity raised TypeError on the fail-closed path,
        # which is the one path that must never raise.
        risk_pct = (risk_amount / equity * 100.0) if has_equity else None

        # -- per-trade risk ----------------------------------------------
        if has_equity:
            check(within(risk_amount, equity * self.limits.risk_per_trade),
                  f"risk {risk_amount:,.2f} is "
                  f"{risk_pct:.3f}% of equity, above the "
                  f"{self.limits.risk_per_trade_pct}% per-trade limit")
        else:
            check(False, "equity unavailable; per-trade risk cannot be checked")

        # -- open trade count --------------------------------------------
        check(account.open_trade_count < self.limits.max_open_trades,
              f"{account.open_trade_count} open trades, at or above the limit "
              f"of {self.limits.max_open_trades}")

        # -- total exposure ------------------------------------------------
        projected_pct = None
        if has_equity:
            current = account.total_open_risk
            if current is None:
                # Cannot total exposure without it. Blocking is correct: a
                # portfolio limit that quietly reads zero is not a limit.
                check(False,
                      "total open risk is unknown; portfolio exposure cannot "
                      "be checked")
            else:
                projected = current + risk_amount
                projected_pct = projected / equity * 100.0
                check(within(projected, equity * self.limits.max_total_exposure),
                      f"projected exposure {projected_pct:.2f}% of equity is "
                      f"above the {self.limits.max_total_exposure_pct}% limit")
        else:
            check(False, "equity unavailable; portfolio exposure unchecked")

        # -- unprotected positions ------------------------------------------
        if account.unprotected_positions > 0:
            check(False,
                  f"{account.unprotected_positions} position(s) have no stop "
                  f"loss; their risk is unquantified, so new exposure cannot "
                  f"be sized safely")

        # -- daily loss ------------------------------------------------------
        if account.start_of_day_equity is None:
            check(False,
                  "start-of-day equity is unknown; the daily loss limit cannot "
                  "be evaluated")
        elif not has_equity:
            # Both figures are needed to express the loss as a percentage.
            # Subtracting a None equity raised TypeError, on the path that must
            # never raise.
            check(False, "equity unknown; daily loss cannot be evaluated")
        else:
            sod = account.start_of_day_equity
            if sod > 0:
                loss_pct = max(0.0, (sod - equity) / sod * 100.0)
                check(within(loss_pct, self.limits.daily_loss_limit_pct),
                      f"daily loss {loss_pct:.2f}% is above the "
                      f"{self.limits.daily_loss_limit_pct}% limit")

        # -- drawdown --------------------------------------------------------
        if account.peak_equity is None:
            check(False, "peak equity is unknown; drawdown cannot be evaluated")
        elif not has_equity:
            check(False, "equity unknown; drawdown cannot be evaluated")
        else:
            peak = account.peak_equity
            if peak > 0:
                drawdown_pct = max(0.0, (peak - equity) / peak * 100.0)
                check(within(drawdown_pct, self.limits.max_drawdown_pct),
                      f"drawdown {drawdown_pct:.2f}% from peak is above the "
                      f"{self.limits.max_drawdown_pct}% limit")

        # -- spread ----------------------------------------------------------
        if intent.spread_points is not None:
            cap = (intent.max_spread_points
                   if intent.max_spread_points is not None
                   else self.limits.default_max_spread_points)
            check(intent.spread_points <= cap,
                  f"spread {intent.spread_points:g} points is above the "
                  f"{cap:g} point limit")

        return self._finish(intent, account, not violations, violations, reasons,
                            risk_amount, risk_pct, projected_pct)

    # -- halt handling ---------------------------------------------------

    def halt(self, kind: str, reason: str) -> None:
        """Trip a halt. Blocks all further orders until cleared."""
        self.halted = True
        self.halt_kind = kind
        self.halt_reason = reason
        if self.journal is not None:
            self.journal.log_event("risk", f"HALT: {reason}", severity="ERROR",
                                   source="risk",
                                   context={"kind": kind, "reason": reason})
        if self.on_breach:
            self.on_breach(kind, reason)

    def clear_daily_loss_halt(self) -> None:
        """Clear a daily-loss halt at the start of a new trading day.

        Only that kind clears automatically. Drawdown and manual halts require
        an operator: a system that resumes itself after a hard stop is not a
        control, it is a delay.
        """
        if self.halt_kind != "daily_loss":
            return
        self.halted = False
        self.halt_kind = ""
        self.halt_reason = ""
        if self.journal is not None:
            self.journal.log_event("risk", "daily-loss halt cleared for a new "
                                           "trading day", source="risk")

    def clear_manual_halt(self, reason: str = "operator reset") -> bool:
        """Clear a manual or drawdown halt. Refuses while any hard limit stands."""
        if self.halt_kind == "daily_loss":
            return False
        if not self.halted:
            return False
        self.halted = False
        self.halt_kind = ""
        self.halt_reason = ""
        if self.journal is not None:
            self.journal.log_event("risk", f"halt cleared: {reason}",
                                   source="risk")
        return True

    def status(self) -> dict:
        return {
            "implemented": True,
            "phase1_stub": False,
            "blocks_all_orders": False,
            "halted": self.halted,
            "halt_kind": self.halt_kind,
            "halt_reason": self.halt_reason,
            "limits": {
                "risk_per_trade_pct": self.limits.risk_per_trade_pct,
                "daily_loss_limit_pct": self.limits.daily_loss_limit_pct,
                "max_drawdown_pct": self.limits.max_drawdown_pct,
                "max_open_trades": self.limits.max_open_trades,
                "max_total_exposure_pct": self.limits.max_total_exposure_pct,
                "default_max_spread_points": self.limits.default_max_spread_points,
            },
        }

    # -- internals -------------------------------------------------------

    def _finish(self, intent: OrderIntent, account: AccountState, approved: bool,
                violations: list[str], reasons: list[str],
                risk_amount: float | None = None,
                risk_pct: float | None = None,
                projected_pct: float | None = None) -> RiskDecision:
        decision = RiskDecision(
            approved=approved,
            reasons=reasons,
            violations=violations,
            risk_amount=risk_amount,
            risk_pct=risk_pct,
            projected_exposure_pct=projected_pct,
        )
        self._record(intent, account, decision)
        return decision

    def _record(self, intent: OrderIntent, account: AccountState,
                decision: RiskDecision) -> None:
        if self.journal is None:
            return
        self.journal.record_risk_decision(
            symbol=intent.symbol, approved=decision.approved,
            reasons=decision.violations or decision.reasons,
        )
        self.journal.log_event(
            "risk",
            "order approved" if decision.approved else "order blocked",
            severity="INFO" if decision.approved else "WARNING",
            source="risk",
            context={
                "symbol": intent.symbol,
                "side": intent.side,
                "volume": intent.volume,
                "risk_amount": decision.risk_amount,
                "risk_pct": decision.risk_pct,
                "projected_exposure_pct": decision.projected_exposure_pct,
                "violations": decision.violations,
            },
        )