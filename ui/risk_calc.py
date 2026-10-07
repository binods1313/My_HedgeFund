from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Iterable, Mapping


@dataclass(frozen=True)
class SymbolSpec:
    symbol: str
    category: str
    digits: int
    point: float
    tick_size: float
    tick_value_loss: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int = 0


@dataclass(frozen=True)
class RiskEstimate:
    amount: float
    percent: float | None
    unbounded: bool = False


@dataclass(frozen=True)
class PortfolioRisk:
    amount: float
    percent: float | None
    unbounded: bool
    position_risks: tuple[float, ...]


@dataclass(frozen=True)
class MarginCheck:
    verified: bool
    sufficient: bool
    required_margin: float | None
    projected_free_margin: float | None
    projected_margin_level: float | None
    warning: str | None = None


@dataclass(frozen=True)
class ValidationFailure:
    code: str
    message: str


@dataclass(frozen=True)
class ValidationReport:
    failures: tuple[ValidationFailure, ...]
    warnings: tuple[str, ...] = ()
    normalized_lots: float | None = None

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def failure_matrix(self) -> tuple[dict[str, str], ...]:
        return tuple({"code": item.code, "message": item.message} for item in self.failures)


def pip_size(spec: SymbolSpec) -> float:
    """Return the pip-equivalent unit in price terms for the symbol."""
    category = (spec.category or "").lower()
    if category == "forex" and spec.digits in (3, 5):
        return float(spec.point) * 10.0
    return float(spec.point)


def normalize_lots(raw: float, spec: SymbolSpec) -> float:
    """Normalize a lot size to broker step and bounds, floor-based and fail-closed."""
    raw_number = _finite_number(raw)
    if raw_number is None:
        raise ValueError("lots must be finite")
    if spec.volume_step <= 0 or spec.volume_min <= 0 or spec.volume_max < spec.volume_min:
        raise ValueError("invalid broker volume specification")

    try:
        step = Decimal(str(spec.volume_step))
        raw_q = Decimal(str(raw_number))
        floored = (raw_q / step).to_integral_value(rounding=ROUND_DOWN) * step
        v_min = Decimal(str(spec.volume_min))
        v_max = Decimal(str(spec.volume_max))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid lot size or broker volume specification") from exc
    return float(max(v_min, min(floored, v_max)))


def trade_risk(spec: SymbolSpec, lots: float, stop_units: float, equity: float | None = None) -> tuple[float, float | None]:
    """Return (risk_amount, risk_pct), where risk_pct is percent of equity or None when equity is invalid."""
    lots_number = _finite_number(lots)
    stop_number = _finite_number(stop_units)
    if lots_number is None or stop_number is None:
        return math.inf, None
    if stop_number < 0 or lots_number < 0:
        raise ValueError("lots and stop distance must not be negative")
    if spec.tick_size <= 0 or spec.tick_value_loss <= 0 or spec.point <= 0:
        return math.inf, None
    distance = stop_number * pip_size(spec)
    ticks_at_risk = distance / spec.tick_size
    risk_amount = lots_number * ticks_at_risk * spec.tick_value_loss
    risk_pct = None
    equity_number = _finite_number(equity)
    if equity_number is not None and equity_number > 0:
        risk_pct = (risk_amount / equity_number) * 100.0
    return risk_amount, risk_pct


def estimate_open_risk(
    spec: SymbolSpec,
    lots: float,
    stop_units: float,
    equity: float | None = None,
) -> RiskEstimate:
    amount, percent = trade_risk(spec, lots, stop_units, equity)
    return RiskEstimate(amount, percent, unbounded=not math.isfinite(amount))


def portfolio_open_risk(
    positions: Iterable[Mapping[str, object]],
    specs: Mapping[str, SymbolSpec],
    equity: float | None = None,
) -> PortfolioRisk:
    """Sum risk from position entry to SL; any missing SL/spec is unbounded."""
    risks: list[float] = []
    unbounded = False
    for position in positions:
        symbol = str(position.get("symbol", ""))
        spec = specs.get(symbol)
        stop = _number(position.get("sl"), 0.0)
        entry = _number(position.get("entry", position.get("price_open")), 0.0)
        lots = _number(position.get("lots", position.get("volume")), 0.0)
        if stop == 0 or spec is None or lots <= 0 or spec.tick_size <= 0 or spec.tick_value_loss <= 0:
            risks.append(math.inf)
            unbounded = True
            continue
        amount = abs(entry - stop) / spec.tick_size * spec.tick_value_loss * lots
        if not math.isfinite(amount):
            amount = math.inf
            unbounded = True
        risks.append(amount)

    total = math.inf if unbounded else sum(risks)
    percent = None
    if equity is not None and math.isfinite(float(equity)) and float(equity) > 0 and math.isfinite(total):
        percent = total / float(equity) * 100.0
    return PortfolioRisk(total, percent, unbounded, tuple(risks))


def risk_rank(pct: float | None) -> str:
    """Return a simple label for the risk percent to feed UI badges."""
    if pct is None:
        return "N/A"
    if pct <= 1.0:
        return "low"
    if pct <= 2.0:
        return "medium"
    return "high"


def required_margin(spec: SymbolSpec, lots: float, margin_per_lot: float) -> float:
    return float(lots) * float(margin_per_lot)


def verify_margin(
    *,
    free_margin: float | None,
    equity: float | None,
    margin_used: float | None,
    required: float | None,
    margin_buffer: float = 0.0,
    warn_level: float = 100.0,
) -> MarginCheck:
    """Verify margin using broker-calculated required margin; unknown blocks."""
    values = (free_margin, equity, margin_used, required)
    numbers = [_finite_number(value) for value in values]
    if any(value is None for value in numbers):
        return MarginCheck(False, False, None, None, None, "Cannot verify required margin")
    if not 0 <= margin_buffer < 1:
        raise ValueError("margin_buffer must be in [0, 1)")

    free, eq, used, needed = numbers
    projected_free = free - needed
    denominator = used + needed
    level = eq / denominator * 100.0 if eq > 0 and denominator > 0 else None
    sufficient = needed <= free * (1.0 - margin_buffer)
    warning = None
    if level is not None and level < warn_level:
        warning = f"Projected margin level {level:.1f}% is below warning level {warn_level:.1f}%"
    return MarginCheck(True, sufficient, needed, projected_free, level, warning)


def validate_pre_submit(
    *,
    spec: SymbolSpec | None,
    symbol: str,
    lots: float,
    stop_units: float | None,
    equity: float | None,
    required: float | None,
    free_margin: float | None,
    margin_used: float | None,
    engine_state: str | None = None,
    connected: bool = False,
    heartbeat_age_s: float | None = None,
    require_engine_ready: bool = True,
    stale_after_s: float = 15.0,
    whitelist: Iterable[str] | None = None,
    market_open: bool | None = None,
    tick_fresh: bool | None = None,
    open_risk: float = 0.0,
    max_risk_per_trade_pct: float | None = None,
    max_total_risk_pct: float | None = None,
    margin_buffer: float = 0.0,
    warn_level: float = 100.0,
    spread_points: float | None = None,
    max_spread_points: float | None = None,
    slippage: float | None = 0.0,
    max_slippage: float | None = None,
    order_token: str | None = None,
    used_tokens: Iterable[str] = (),
    unprotected_tickets: Iterable[int] | None = None,
) -> ValidationReport:
    """Return all machine-readable pre-submit failures without side effects."""
    failures: list[ValidationFailure] = []
    warnings: list[str] = []

    def fail(code: str, message: str) -> None:
        failures.append(ValidationFailure(code, message))

    state = str(engine_state or "offline").strip().lower()
    heartbeat_age = _finite_number(heartbeat_age_s)
    age_stale = heartbeat_age is None or heartbeat_age > stale_after_s
    if require_engine_ready and (
        not connected
        or state in {"offline", "off", "paused", "halted", "emergency_stop", "stale"}
        or age_stale
    ):
        fail("ENGINE_NOT_READY", "Engine is offline, paused, halted, or has stale telemetry")

    if whitelist is None or symbol.upper() not in {str(item).upper() for item in whitelist}:
        fail("SYMBOL_NOT_WHITELISTED", f"Symbol {symbol!r} is not in the trading whitelist")
    if market_open is not True or tick_fresh is not True:
        fail("MARKET_CLOSED", "Market is closed or no fresh tick is available")

    normalized: float | None = None
    if spec is None:
        fail("LOTS_INVALID", "Broker symbol specification is unavailable")
    else:
        try:
            lots_number = _finite_number(lots)
            if lots_number is None or lots_number <= 0:
                raise ValueError("lots must be finite and greater than zero")
            normalized = normalize_lots(lots_number, spec)
            if normalized <= 0 or normalized < spec.volume_min:
                raise ValueError("lots are below broker minimum after normalization")
        except (TypeError, ValueError, OverflowError):
            fail("LOTS_INVALID", "Lot size is invalid for the broker volume rules")

    stop_number = _finite_number(stop_units)
    if stop_number is None or stop_number <= 0:
        fail("SL_REQUIRED", "A positive stop loss is required")
    elif spec is not None and spec.stops_level_points > 0:
        spread = max(0.0, _finite_number(spread_points) or 0.0)
        minimum_distance = (spec.stops_level_points + spread) * spec.point
        if stop_number * pip_size(spec) < minimum_distance:
            fail("SL_TOO_CLOSE", "Stop loss is inside the broker minimum distance including spread")

    equity_number = _finite_number(equity)
    if equity_number is None or equity_number <= 0:
        fail("EQUITY_INVALID", "Equity is unavailable or not positive; risk cannot be verified")
    elif spec is not None and stop_number is not None and stop_number > 0 and normalized is not None:
        risk_amount, risk_pct = trade_risk(spec, normalized, stop_number, equity_number)
        if not math.isfinite(risk_amount) or risk_pct is None:
            fail("RISK_PER_TRADE_EXCEEDED", "Trade risk cannot be calculated from broker specifications")
        elif max_risk_per_trade_pct is not None and risk_pct > max_risk_per_trade_pct:
            fail("RISK_PER_TRADE_EXCEEDED", f"Trade risk {risk_pct:.2f}% exceeds {max_risk_per_trade_pct:.2f}%")
        if max_total_risk_pct is not None:
            open_risk_number = _finite_number(open_risk)
            # _finite_number collapses inf/NaN to None. Keep the same fail/pass
            # test, and only change the sentence when the raw value is unbounded.
            unbounded_open_risk = False
            if open_risk is not None and not isinstance(open_risk, bool):
                try:
                    unbounded_open_risk = not math.isfinite(float(open_risk))
                except (TypeError, ValueError, OverflowError):
                    unbounded_open_risk = False
            if open_risk_number is None or open_risk_number + risk_amount > equity_number * max_total_risk_pct / 100.0:
                if unbounded_open_risk:
                    listed = ", ".join(f"#{int(ticket)}" for ticket in (unprotected_tickets or [])) or "—"
                    fail(
                        "TOTAL_RISK_EXCEEDED",
                        "Open positions without a stop loss are counted as unlimited risk. "
                        f"Tickets: {listed}",
                    )
                else:
                    fail("TOTAL_RISK_EXCEEDED", "Open portfolio risk plus this trade exceeds the configured cap")

    margin = verify_margin(
        free_margin=free_margin,
        equity=equity,
        margin_used=margin_used,
        required=required,
        margin_buffer=margin_buffer,
        warn_level=warn_level,
    )
    if not margin.verified or not margin.sufficient:
        fail("MARGIN_INSUFFICIENT", margin.warning or "Cannot verify sufficient free margin")
    if margin.warning:
        warnings.append(margin.warning)

    spread_number = _finite_number(spread_points)
    if max_spread_points is not None and (spread_number is None or spread_number > max_spread_points):
        fail("SPREAD_TOO_WIDE", "Spread exceeds the configured maximum or is unavailable")
    slippage_number = _finite_number(slippage)
    if slippage is not None and (slippage_number is None or slippage_number < 0 or (max_slippage is not None and slippage_number > max_slippage)):
        fail("SLIPPAGE_INVALID", "Slippage is negative, invalid, or above the configured maximum")
    if order_token is not None and order_token in set(used_tokens):
        fail("DUPLICATE_SUBMIT", "This idempotency token has already been used")

    return ValidationReport(tuple(failures), tuple(warnings), normalized)


def _number(value: object, default: float) -> float:
    return _finite_number(value) if _finite_number(value) is not None else default


_HARD_RISK_CODES = frozenset({
    "TOTAL_RISK_EXCEEDED",
    "MARGIN_INSUFFICIENT",
    "SL_REQUIRED",
    "STOP_TOO_CLOSE",
})


def risk_card_accent(codes: list[str] | tuple[str, ...]) -> str:
    """Green only when every check passed. Any shown code is amber or red."""
    if not codes:
        return "green"
    for code in codes:
        upper = str(code).upper()
        if (
            upper in _HARD_RISK_CODES
            or "RISK" in upper
            or "MARGIN" in upper
            or "STOP" in upper
            or upper.startswith("SL")
        ):
            return "red"
    return "amber"


def risk_summary_level(accent: str, risk_pct: float | None) -> str:
    """Streamlit level for the risk sentence. Success only when every check passed.

    A missing percent stays informational. The card accent still carries the
    failure colour, and a known percent is never shown as success beside a code.
    """
    if risk_pct is None or (isinstance(risk_pct, float) and risk_pct != risk_pct):
        return "info"
    if accent == "red" or risk_pct > 1.0:
        return "error"
    if accent == "amber":
        return "warning"
    return "success"


def _finite_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def ensure_valid_lots(raw: float, spec: SymbolSpec) -> float:
    normalized = normalize_lots(raw, spec)
    if normalized <= 0:
        raise ValueError("lots must be > 0 after normalization")
    return normalized
