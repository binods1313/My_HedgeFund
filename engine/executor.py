"""Order execution.

PaperExecutor and LiveExecutor share one interface so that testing PAPER proves
something about the surrounding machinery. If they diverged, a passing PAPER run
would say nothing about the live path.

Invariants enforced here rather than at call sites:

  - Every order carries a broker-side stop loss. An order without one is never
    submitted.
  - An order cannot be submitted unless the risk gate approved it.
  - EMERGENCY_STOP and PAUSED block submission.
  - LIVE mode requires an explicit confirmation in addition to the config flag.

The Phase 1 risk stub rejects everything, so no trade can reach the broker before
Phase 3 ships a real gate. That is deliberate: a stub that approved would be
dangerous.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from engine.gateway import Gateway, OrderOutcome, OrderResult
from engine.journal import (
    STATE_EMERGENCY_STOP,
    STATE_PAUSED,
    TRADE_CLOSED,
    TRADE_FAILED,
    TRADE_OPEN,
    Journal,
    utc_now_iso,
)

MODE_PAPER = "PAPER"
MODE_LIVE = "LIVE"

# Confirmation phrase required before LIVE may be used.
LIVE_CONFIRMATION_PHRASE = "ENABLE LIVE TRADING"


class ExecutionRejected(RuntimeError):
    """An order was refused before submission."""


@dataclass(frozen=True)
class OrderRequest:
    """A validated intent to trade. Constructed by prepare_order()."""

    symbol: str
    side: str                 # BUY | SELL
    volume: float
    sl: float
    tp: float | None = None
    comment: str = ""
    magic: int = 100100

    def __post_init__(self):
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {self.side!r}")
        if self.volume <= 0:
            raise ValueError(f"volume must be > 0, got {self.volume}")


@dataclass
class ExecutionResult:
    ok: bool
    status: str
    ticket: int | None = None
    price: float | None = None
    message: str = ""
    resolution: str | None = None
    mode: str = MODE_PAPER


@dataclass(frozen=True)
class RiskApproval:
    """Proof that the risk gate approved a specific order.

    submit() takes this rather than a bool. A bare True lets any caller assert
    approval without ever consulting the gate, which is precisely the shortcut
    the safety design is meant to prevent. The identifier of the decision it
    came from is carried so the journal entry and the order can be correlated.
    """

    approved: bool
    decision_id: str = ""
    symbol: str = ""
    risk_amount: float | None = None
    risk_pct: float | None = None
    reasons: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.approved

    @classmethod
    def from_decision(cls, decision, symbol: str = "") -> "RiskApproval":
        return cls(
            approved=bool(decision.approved),
            decision_id=f"{decision.approved}:{symbol}",
            symbol=symbol,
            risk_amount=getattr(decision, "risk_amount", None),
            risk_pct=getattr(decision, "risk_pct", None),
            reasons=tuple(getattr(decision, "violations", ()) or ()),
        )


@dataclass
class Executor(ABC):
    """Base for both execution modes."""

    mode: str = MODE_PAPER
    journal: Journal | None = None
    gateway: Gateway | None = None

    @abstractmethod
    def prepare_order(self, symbol: str, side: str, volume: float, sl: float,
                      tp: float | None = None, comment: str = "") -> OrderRequest:
        """Build and validate an OrderRequest. Pure: no I/O."""

    @abstractmethod
    def submit(self, request: OrderRequest,
               risk_approved: RiskApproval) -> ExecutionResult:
        """Send a prepared order, subject to every gate."""

    @abstractmethod
    def close(self, ticket: int) -> ExecutionResult:
        """Close an open position."""

    # -- shared gates ----------------------------------------------------

    @staticmethod
    def _check_sl(request: OrderRequest) -> None:
        """Refuse an order without a stop loss.

        Without an SL there is no defined worst case, so the position size cannot
        be bounded and the exposure is unbounded. This check is deliberately not
        a warning.
        """
        if request.sl is None or request.sl <= 0:
            raise ExecutionRejected(
                f"Refusing to submit {request.side} {request.symbol}: no stop "
                f"loss. Every order requires a broker-side SL."
            )

    @staticmethod
    def _check_risk(risk_approved) -> None:
        """Refuse an order without a real approval from the gate.

        Accepts only a RiskApproval that the gate produced. A plain True is
        refused, because that lets a caller bypass the gate entirely -- the
        failure mode this check exists to close.
        """
        if isinstance(risk_approved, RiskApproval):
            if not risk_approved.approved:
                reasons = "; ".join(risk_approved.reasons) or "no reason recorded"
                raise ExecutionRejected(
                    f"Refusing to submit: risk gate did not approve this "
                    f"order. Reasons: {reasons}"
                )
            return
        if isinstance(risk_approved, bool):
            raise ExecutionRejected(
                "Refusing to submit: a bare True is not risk approval. Use "
                "RiskApproval.from_decision(decision) so the gate's verdict is "
                "what authorises the order."
            )
        raise ExecutionRejected(
            f"Refusing to submit: risk approval must be a RiskApproval, got "
            f"{type(risk_approved).__name__}"
        )

    @staticmethod
    def _check_state(state: str | None) -> None:
        """Block submission while halted.

        EMERGENCY_STOP and PAUSED both block. A live position is still closable
        in those states: closing reduces risk and must never be gated.
        """
        if state == STATE_EMERGENCY_STOP:
            raise ExecutionRejected(
                "Refusing to submit: engine is in EMERGENCY_STOP."
            )
        if state == STATE_PAUSED:
            raise ExecutionRejected("Refusing to submit: engine is PAUSED.")


@dataclass
class PaperExecutor(Executor):
    """Simulated fills against live quotes, positions persisted in the DB."""

    mode: str = MODE_PAPER
    journal: Journal | None = None
    gateway: Gateway | None = None
    state: str | None = None
    tick_provider: Any = None
    _next_ticket: int = 9_000_000
    _positions: dict[int, dict] = field(default_factory=dict)

    def prepare_order(self, symbol: str, side: str, volume: float, sl: float,
                      tp: float | None = None, comment: str = "") -> OrderRequest:
        return OrderRequest(symbol=symbol.upper(), side=side.upper(),
                            volume=float(volume), sl=float(sl),
                            tp=float(tp) if tp else None,
                            comment=comment or "paper")

    def _quote(self, symbol: str) -> tuple[float, float]:
        if self.tick_provider is None:
            raise ExecutionRejected("PaperExecutor has no tick provider")
        tick = self.tick_provider(symbol)
        if tick is None:
            raise ExecutionRejected(f"no quote for {symbol}")
        return float(tick.bid), float(tick.ask)

    def submit(self, request: OrderRequest,
               risk_approved: RiskApproval) -> ExecutionResult:
        self._check_sl(request)
        self._check_risk(risk_approved)
        self._check_state(self.state)

        bid, ask = self._quote(request.symbol)
        price = ask if request.side == "BUY" else bid

        self._next_ticket += 1
        ticket = self._next_ticket

        position = {
            "ticket": ticket,
            "symbol": request.symbol,
            "side": request.side,
            "volume": request.volume,
            "price_open": price,
            "price_current": price,
            "sl": request.sl,
            "tp": request.tp,
            "profit": 0.0,
            "magic": request.magic,
            "status": TRADE_OPEN,
            "ts": utc_now_iso(),
        }
        self._positions[ticket] = position

        if self.journal:
            self.journal.upsert_position(
                ticket=ticket, symbol=request.symbol, side=request.side,
                volume=request.volume, price_open=price, price_current=price,
                sl=request.sl, tp=request.tp, profit=0.0, magic=request.magic,
            )
            self.journal.execute(
                "INSERT INTO trades (ticket, symbol, side, volume, entry_price, "
                "sl, tp, status, magic, broker, ts, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ticket, request.symbol, request.side, request.volume, price,
                 request.sl, request.tp, TRADE_OPEN, request.magic, "PAPER",
                 position["ts"], position["ts"]),
            )
            self.journal.log_event("trade", "paper order filled",
                                   source="executor",
                                   context={"ticket": ticket,
                                            "symbol": request.symbol,
                                            "price": price})

        return ExecutionResult(ok=True, status=TRADE_OPEN, ticket=ticket,
                               price=price, mode=MODE_PAPER,
                               message="simulated fill")

    def close(self, ticket: int) -> ExecutionResult:
        position = self._positions.get(int(ticket))
        if position is None and self.journal:
            rows = self.journal.query(
                "SELECT * FROM positions WHERE ticket = ?", (int(ticket),))
            position = dict(rows[0]) if rows else None
        if position is None:
            return ExecutionResult(ok=False, status="NOT_FOUND",
                                   ticket=int(ticket), mode=MODE_PAPER,
                                   message="no such open position")

        bid, ask = self._quote(position["symbol"])
        price = bid if position["side"] == "BUY" else ask
        profit = (price - position["price_open"]) * position["volume"] \
            if position["side"] == "BUY" \
            else (position["price_open"] - price) * position["volume"]

        self._positions.pop(int(ticket), None)
        if self.journal:
            self.journal.delete_position(int(ticket))
            self.journal.execute(
                "UPDATE trades SET status = ?, exit_price = ?, updated_at = ? "
                "WHERE ticket = ?",
                (TRADE_CLOSED, price, utc_now_iso(), int(ticket)))
            self.journal.log_event("trade", "paper position closed",
                                   source="executor",
                                   context={"ticket": int(ticket),
                                            "profit": profit})

        return ExecutionResult(ok=True, status=TRADE_CLOSED,
                               ticket=int(ticket), price=price,
                               mode=MODE_PAPER, message="simulated close")

    def positions(self) -> list[dict]:
        return list(self._positions.values())

    def on_tick(self, symbol: str) -> list[dict]:
        """Re-evaluate open positions: mark to market, honour SL and TP.

        Called each quote cycle by PAPER mode. Returns positions that closed on
        this tick.
        """
        closed: list[dict] = []
        for ticket, position in list(self._positions.items()):
            if position["symbol"] != symbol:
                continue
            try:
                bid, ask = self._quote(symbol)
            except ExecutionRejected:
                continue
            price = bid if position["side"] == "BUY" else ask
            position["price_current"] = price

            hit_sl = position["sl"] and (
                price <= position["sl"] if position["side"] == "BUY"
                else price >= position["sl"])
            hit_tp = position["tp"] and (
                price >= position["tp"] if position["side"] == "BUY"
                else price <= position["tp"])

            if hit_sl or hit_tp:
                result = self.close(ticket)
                result.message = "closed on stop loss" if hit_sl else "closed on take profit"
                closed.append({"ticket": ticket, "reason": result.message,
                               "result": result})
            elif self.journal:
                profit = (price - position["price_open"]) * position["volume"] \
                    if position["side"] == "BUY" \
                    else (position["price_open"] - price) * position["volume"]
                position["profit"] = profit
                self.journal.upsert_position(
                    ticket=ticket, symbol=position["symbol"],
                    side=position["side"], volume=position["volume"],
                    price_open=position["price_open"], price_current=price,
                    sl=position["sl"], tp=position["tp"], profit=profit,
                    magic=position["magic"],
                )
        return closed


@dataclass
class LiveExecutor(Executor):
    """Submits through the gateway, so reconciliation and guards apply."""

    mode: str = MODE_LIVE
    journal: Journal | None = None
    gateway: Gateway | None = None
    state: str | None = None
    live_confirmed: bool = False

    def __post_init__(self):
        if self.mode == MODE_LIVE and not self.live_confirmed:
            raise ExecutionRejected(
                "LIVE mode requires live_confirmed=True in addition to the "
                "config flag. A flag alone must never enable live trading."
            )

    def prepare_order(self, symbol: str, side: str, volume: float, sl: float,
                      tp: float | None = None, comment: str = "") -> OrderRequest:
        return OrderRequest(symbol=symbol.upper(), side=side.upper(),
                            volume=float(volume), sl=float(sl),
                            tp=float(tp) if tp else None,
                            comment=comment or "engine")

    def submit(self, request: OrderRequest,
               risk_approved: RiskApproval) -> ExecutionResult:
        self._check_sl(request)
        self._check_risk(risk_approved)
        self._check_state(self.state)

        if self.gateway is None:
            raise ExecutionRejected("LiveExecutor has no gateway")

        tick = self.gateway.symbol_info_tick(request.symbol)
        if tick is None:
            raise ExecutionRejected(f"no quote for {request.symbol}")
        price = tick.ask if request.side == "BUY" else tick.bid

        payload = {
            "symbol": request.symbol,
            "volume": request.volume,
            "type": request.side,
            "price": price,
            "sl": request.sl,
            "tp": request.tp or 0.0,
            "comment": request.comment,
            "magic": request.magic,
        }
        result: OrderResult = self.gateway.send_order(payload)

        if result.must_halt:
            self._log("executor", "order outcome UNKNOWN; halting",
                      severity="ERROR",
                      context={"symbol": request.symbol,
                               "resolution": result.resolution.value,
                               "comment": result.comment})
            return ExecutionResult(
                ok=False, status="UNKNOWN", mode=MODE_LIVE,
                resolution=result.resolution.value,
                message="order fate unknown; engine must halt and alert",
            )

        if result.ok:
            if self.journal:
                self.journal.upsert_position(
                    ticket=result.ticket, symbol=request.symbol,
                    side=request.side, volume=request.volume,
                    price_open=price, price_current=price, sl=request.sl,
                    tp=request.tp, profit=0.0, magic=request.magic,
                )
            return ExecutionResult(ok=True, status=TRADE_OPEN,
                                   ticket=result.ticket, price=price,
                                   mode=MODE_LIVE, message=result.comment)

        return ExecutionResult(
            ok=False, status="REJECTED", mode=MODE_LIVE,
            resolution=result.resolution.value,
            message=f"{result.comment} (retcode={result.retcode})")

    def close(self, ticket: int) -> ExecutionResult:
        if self.gateway is None:
            raise ExecutionRejected("LiveExecutor has no gateway")
        # Closing is permitted even while halted: it reduces risk.
        result = self.gateway.close_position(ticket)
        if result.ok and self.journal:
            self.journal.delete_position(int(ticket))
            self.journal.execute(
                "UPDATE trades SET status = ?, updated_at = ? WHERE ticket = ?",
                (TRADE_CLOSED, utc_now_iso(), int(ticket)))
        return ExecutionResult(
            ok=result.ok, status=TRADE_CLOSED if result.ok else "REJECTED",
            ticket=int(ticket), mode=MODE_LIVE,
            resolution=result.resolution.value, message=result.comment)

    def _log(self, category: str, message: str, *, severity: str = "INFO",
             context: dict | None = None) -> None:
        if self.journal:
            self.journal.log_event(category, message, severity=severity,
                                   source="executor", context=context)


def build_executor(mode: str, *, journal=None, gateway=None, state: str | None = None,
                   tick_provider=None, live_confirmed: bool = False) -> Executor:
    """Construct the executor for `mode`.

    PAPER is the default and requires nothing extra. LIVE requires both the
    config flag and an explicit confirmation, so neither alone is sufficient.
    """
    if mode == MODE_PAPER:
        return PaperExecutor(journal=journal, gateway=gateway, state=state,
                             tick_provider=tick_provider)
    if mode == MODE_LIVE:
        if not live_confirmed:
            raise ExecutionRejected(
                "Refusing to construct a LiveExecutor: live confirmation has "
                "not been supplied."
            )
        return LiveExecutor(journal=journal, gateway=gateway, state=state,
                            live_confirmed=True)
    raise ExecutionRejected(f"unknown mode {mode!r}; expected PAPER or LIVE")