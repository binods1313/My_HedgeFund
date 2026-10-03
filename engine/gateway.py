"""MT5 gateway: the only engine module permitted to touch the MT5 API.

One thread owns the connection. `mt5.initialize()` and `login()` run inside that
thread, never at import, because MT5's API is not thread-safe and initialising
from a worker other than the owner has produced intermittent failures.

Three behaviours here exist because of specific failure modes:

**Timeout raises. It never returns None.** A silent None is indistinguishable
from "the call returned nothing", which is how a ghost order appears: the caller
concludes the order did not go through and retries, while the broker processes
the original.

**A timed-out order is reconciled, not retried.** If `order_send` times out the
order may still have executed. The gateway resolves it to EXECUTED,
NOT_EXECUTED, or UNKNOWN. UNKNOWN halts and alerts.

**Disconnect blocks trading.** While `connected` is False the gateway refuses
send_order outright rather than queueing into a terminal that may not be there.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

DEFAULT_CALL_TIMEOUT = 10.0
DEFAULT_BACKOFF = (1, 2, 4, 8, 16, 30)
DEFAULT_ORDER_LOOKBACK = 120


class GatewayError(RuntimeError):
    """Base for gateway failures."""


class NotConnected(GatewayError):
    """Raised when an operation needs a live connection and there is none."""

    def __init__(self, reason: str = "not connected"):
        self.reason = reason
        super().__init__(f"MT5 gateway is not connected: {reason}")


class MT5Timeout(TimeoutError):
    """A call exceeded its deadline.

    Subclasses TimeoutError deliberately so existing Phase 0 error handling,
    which catches TimeoutError, keeps working.
    """

    def __init__(self, operation: str, seconds: float):
        self.operation = operation
        self.seconds = seconds
        super().__init__(
            f"MT5 call {operation!r} timed out after {seconds:g}s. "
            f"The call may still have been processed by the terminal."
        )


class OrderOutcome(str, Enum):
    """Resolution of an order whose submission did not return cleanly."""

    EXECUTED = "EXECUTED"
    NOT_EXECUTED = "NOT_EXECUTED"
    UNKNOWN = "UNKNOWN"      # never retry, never assume; halt and alert


class Resolution(str, Enum):
    """What the caller should do next."""

    PROCEED = "PROCEED"
    RETRY_SAFE = "RETRY_SAFE"
    HALT = "HALT"


@dataclass(frozen=True)
class OrderResult:
    """Outcome of a send attempt."""

    outcome: OrderOutcome
    resolution: Resolution
    ticket: int | None = None
    retcode: int | None = None
    comment: str = ""
    position: dict | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is OrderOutcome.EXECUTED

    @property
    def should_retry(self) -> bool:
        return self.resolution is Resolution.RETRY_SAFE

    @property
    def must_halt(self) -> bool:
        return self.resolution is Resolution.HALT


@dataclass
class GatewayStatus:
    connected: bool = False
    last_error: str = ""
    consecutive_failures: int = 0
    backoff_seconds: float = 0.0
    next_retry_at: float = 0.0
    thread_alive: bool = False


class Gateway:
    """Serialises all MT5 access onto one owned thread.

    Callers submit (fn, args, kwargs) and block on a per-request event until the
    owner thread answers or the deadline passes. Nothing else in the engine may
    import MetaTrader5.
    """

    def __init__(self, *, login: int | None = None, password: str | None = None,
                 server: str | None = None, terminal_path: str | None = None,
                 magic: int = 100100, call_timeout: float = DEFAULT_CALL_TIMEOUT,
                 backoff: tuple[int, ...] = DEFAULT_BACKOFF,
                 order_lookback: int = DEFAULT_ORDER_LOOKBACK,
                 mt5_module: Any = None,
                 journal=None):
        self.login = login
        self.password = password
        self.server = server
        self.terminal_path = terminal_path
        self.magic = magic
        self.call_timeout = call_timeout
        self.backoff = backoff
        self.order_lookback = order_lookback

        # Injected for tests. In production this is the real module, imported
        # lazily inside the owner thread.
        self._mt5 = mt5_module
        self.journal = journal

        self.status = GatewayStatus()
        self._queue: "queue.Queue[_Request | None]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connect_lock = threading.Lock()
        self._subscriber = None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Start the owner thread. It initialises and logs in itself."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="mt5-gateway", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=timeout)
        self._thread = None
        self.status.connected = False

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                break
            self._handle(item)

    def _handle(self, request: "_Request") -> None:
        """Service one request. Runs on the owner thread.

        The MT5 call is made inline, so MT5 is only ever touched by its owner.
        An earlier implementation spawned a fresh thread per call to avoid being
        blocked by a hang, which meant MT5 was driven from a different thread on
        every request: precisely what this design exists to prevent.

        The consequence is deliberate. If order_send blocks, the owner thread is
        stuck, the caller's deadline fires, and the outcome is UNKNOWN + HALT.
        That is the honest answer rather than a guess, because a blocked owner
        means the terminal cannot be read for reconciliation either.
        """
        # Reconnect before servicing a request, so callers never see a stale
        # failure when the link is simply down.
        if not self.status.connected:
            if not self._owner_ensure_connected():
                request.reject(NotConnected(self.status.last_error))
                return
        try:
            result = request.fn(*request.args, **request.kwargs)
        except Exception as exc:      # noqa: BLE001 - recorded and reported
            self.status.consecutive_failures += 1
            self.status.last_error = f"{type(exc).__name__}: {exc}"
            request.reject(exc)
            return
        self.status.consecutive_failures = 0
        self.status.last_error = ""
        request.resolve(result)

    # -- connection, always on the owner thread ---------------------------

    def ensure_connected(self, timeout: float | None = None) -> bool:
        """Public entry point. Asks the OWNER THREAD to connect.

        A caller must never call _ensure_connected directly: that would run
        initialize() and login() on the calling thread, and MT5's API is not
        thread-safe. The original implementation did exactly that from
        make_gateway(), which is how the single-thread guarantee was broken.
        """
        request = _Request("ensure_connected", lambda: True, (), {},
                           timeout=self.call_timeout if timeout is None else timeout)
        self._queue.put(request)
        try:
            request.wait()
        except Exception:   # noqa: BLE001 - a failed connect is not an exception
            return False   # here; it is reported through status.connected.
        if request.error is not None:
            return False
        return self.status.connected

    def _owner_ensure_connected(self) -> bool:
        """Connect. MUST be called on the owner thread."""
        if self.status.connected:
            return True
        if not self._thread or threading.get_ident() == self._thread.ident:
            # Already on the owner: run inline.
            return self._ensure_connected()
        # Called from elsewhere: hand it to the owner thread.
        request = _Request("ensure_connected", lambda: True, (), {},
                           timeout=self.call_timeout)
        self._queue.put(request)
        try:
            request.wait()
        except Exception:   # noqa: BLE001 - reported via status.connected
            return False
        return self.status.connected

    # -- the actual call -------------------------------------------------

    def _invoke(self, request: "_Request") -> Any:
        """Execute inline on the owner thread.

        No worker is spawned. The deadline lives on the CALLER's wait (see
        _Request.wait), not here, so a blocked MT5 call blocks the owner thread
        and the caller resolves to UNKNOWN rather than being told 'failed'.
        """
        return request.fn(*request.args, **request.kwargs)

    # -- request helpers -------------------------------------------------

    def _submit(self, name: str, fn: Callable, *args,
                timeout: float | None = None, **kwargs) -> Any:
        if not self.status.connected:
            raise NotConnected(self.status.last_error or "not started")
        request = _Request(name, fn, args, kwargs,
                           timeout=self.call_timeout if timeout is None else timeout)
        self._queue.put(request)
        return request.wait()

    def call(self, name: str, fn: Callable, *args, timeout: float | None = None,
             **kwargs) -> Any:
        """Run an arbitrary callable on the owner thread.

        Used for reads: account_info, symbol_info, positions_get and so on. An
        order submission must go through send_order() so reconciliation runs.
        """
        return self._submit(name, fn, *args, timeout=timeout, **kwargs)

    # -- connection ------------------------------------------------------

    def _ensure_connected(self) -> bool:
        with self._connect_lock:
            if self.status.connected:
                return True

            now = time.monotonic()
            if now < self.status.next_retry_at:
                return False

            mt5 = self._resolve_mt5()
            if mt5 is None:
                self.status.last_error = "MetaTrader5 package unavailable"
                self.status.connected = False
                return False

            try:
                kwargs = {}
                if self.terminal_path:
                    kwargs["terminal_path"] = self.terminal_path
                if not mt5.initialize(**kwargs):
                    self.status.last_error = (
                        f"initialize() failed: {getattr(mt5, 'last_error', lambda: '?')()}")
                    self.status.connected = False
                    self._schedule_backoff()
                    return False

                if self.login is not None:
                    ok = mt5.login(self.login, password=self.password,
                                   server=self.server)
                    if not ok:
                        self.status.last_error = (
                            f"login() failed: {getattr(mt5, 'last_error', lambda: '?')()}")
                        mt5.shutdown()
                        self.status.connected = False
                        self._schedule_backoff()
                        return False

                self.status.connected = True
                self.status.last_error = ""
                self.status.consecutive_failures = 0
                self.status.backoff_seconds = 0.0
                self._log("gateway", "connected to MT5 terminal",
                          context={"server": self.server, "login": self.login})
                return True
            except Exception as exc:   # noqa: BLE001
                self.status.last_error = f"{type(exc).__name__}: {exc}"
                self.status.connected = False
                self._schedule_backoff()
                return False

    def _schedule_backoff(self) -> None:
        """Exponential backoff with a cap. Raises a reconnect deadline.

        The first failure waits backoff[0]. `consecutive_failures` is incremented
        before this is called, so index is offset by one; without that the first
        retry waited twice as long as configured.
        """
        idx = max(0, min(self.status.consecutive_failures - 1,
                         len(self.backoff) - 1))
        delay = float(self.backoff[idx])
        self.status.backoff_seconds = delay
        self.status.next_retry_at = time.monotonic() + delay
        self._log("gateway", "reconnect scheduled",
                  severity="WARNING",
                  context={"delay_seconds": delay,
                           "error": self.status.last_error})

    def _resolve_mt5(self):
        if self._mt5 is not None:
            return self._mt5
        try:
            import MetaTrader5 as mt5
        except ImportError:
            return None
        self._mt5 = mt5
        return mt5

    # -- order submission with reconciliation ---------------------------

    def send_order(self, request: dict) -> OrderResult:
        """Submit an order, reconciling if the call times out.

        On timeout the order may still have executed, so this consults
        history_orders_get() and positions_get() before concluding anything. It
        never retries on its own: a blind retry after a timeout is how duplicate
        positions appear.
        """
        if not self.status.connected:
            return OrderResult(
                outcome=OrderOutcome.NOT_EXECUTED,
                resolution=Resolution.RETRY_SAFE,
                comment=f"not connected: {self.status.last_error}",
            )

        request = dict(request)
        request.setdefault("magic", self.magic)

        try:
            result = self._submit("order_send", self._mt5_or_raise().order_send, request,
                                  timeout=self.call_timeout)
        except MT5Timeout:
            return self._reconcile_after_timeout(request)
        except NotConnected as exc:
            return OrderResult(
                outcome=OrderOutcome.NOT_EXECUTED,
                resolution=Resolution.RETRY_SAFE,
                comment=str(exc),
            )
        except Exception as exc:   # noqa: BLE001
            return self._reconcile_after_error(request, exc)

        return self._interpret_send_result(request, result)

    def _interpret_send_result(self, request: dict, result: Any) -> OrderResult:
        if result is None:
            # MT5 returned nothing. Could be a rejected send or a transport
            # problem; treat it as unknown rather than assuming failure.
            return self._reconcile_after_timeout(request)

        retcode = getattr(result, "retcode", None)
        comment = getattr(result, "comment", "") or ""
        ticket = getattr(result, "order", None) or getattr(result, "deal", None)

        if retcode == self._retcode("TRADE_RETCODE_DONE", 10009):
            return OrderResult(
                outcome=OrderOutcome.EXECUTED,
                resolution=Resolution.PROCEED,
                ticket=int(ticket) if ticket else None,
                retcode=int(retcode),
                comment=comment,
            )

        # A definite broker rejection is safe to retry after adjustment.
        return OrderResult(
            outcome=OrderOutcome.NOT_EXECUTED,
            resolution=Resolution.RETRY_SAFE,
            retcode=int(retcode) if retcode is not None else None,
            comment=comment,
        )

    def _reconcile_after_timeout(self, request: dict) -> OrderResult:
        """Determine what actually happened to a timed-out order.

        This is the whole reason a timeout raises rather than returning None.
        Without it the caller cannot distinguish 'never sent' from 'sent and
        filled', and the retry that follows creates a duplicate position.
        """
        symbol = request.get("symbol")
        magic = request.get("magic", self.magic)
        self._log("gateway", "order submission timed out; reconciling",
                  severity="WARNING",
                  context={"symbol": symbol, "magic": magic})

        try:
            positions = self._reconcile_positions(symbol, magic)
            history = self._reconcile_history(symbol, magic)
        except Exception as exc:   # noqa: BLE001
            # Reconciliation itself failed. The order's fate is genuinely
            # unknown, so this must halt rather than retry.
            self._log("gateway", "reconciliation failed; order fate UNKNOWN",
                      severity="ERROR", context={"error": str(exc)})
            return OrderResult(
                outcome=OrderOutcome.UNKNOWN,
                resolution=Resolution.HALT,
                comment=f"reconciliation failed: {exc}",
            )

        if positions:
            ticket = positions[0].get("ticket")
            self._log("gateway", "timed-out order was in fact executed",
                      context={"ticket": ticket, "symbol": symbol})
            return OrderResult(
                outcome=OrderOutcome.EXECUTED,
                resolution=Resolution.PROCEED,
                ticket=int(ticket) if ticket else None,
                position=positions[0],
                comment="recovered by reconciliation after timeout",
            )

        if history:
            ticket = history[0].get("ticket")
            self._log("gateway", "timed-out order found in history but no position",
                      severity="WARNING",
                      context={"ticket": ticket, "symbol": symbol})
            return OrderResult(
                outcome=OrderOutcome.EXECUTED,
                resolution=Resolution.PROCEED,
                ticket=int(ticket) if ticket else None,
                position=None,
                comment="order present in history; position already closed",
            )

        # No trace in either position or history. Safe to retry.
        self._log("gateway", "timed-out order left no trace; retry is safe",
                  context={"symbol": symbol})
        return OrderResult(
            outcome=OrderOutcome.NOT_EXECUTED,
            resolution=Resolution.RETRY_SAFE,
            comment="no trace after timeout; safe to retry",
        )

    def _reconcile_after_error(self, request: dict, exc: Exception) -> OrderResult:
        """A non-timeout exception may still have reached the broker."""
        self._log("gateway", "order submission raised; reconciling",
                  severity="WARNING",
                  context={"error": str(exc), "symbol": request.get("symbol")})
        return self._reconcile_after_timeout(request)

    def _reconcile_positions(self, symbol: str | None, magic: int) -> list[dict]:
        # No `or []` fallback: an empty result and a failed call are different
        # facts, and conflating them is how a lost order becomes a duplicate.
        # positions_get() omits None arguments deliberately; see its docstring.
        positions = self.positions_get(symbol=symbol)
        if positions is None:
            raise GatewayError(
                "positions_get returned None during reconciliation; the order's "
                "fate cannot be determined"
            )
        out = []
        for p in positions:
            if magic is not None and getattr(p, "magic", None) not in (magic, None):
                continue
            out.append({
                "ticket": getattr(p, "ticket", None),
                "symbol": getattr(p, "symbol", symbol),
                "volume": getattr(p, "volume", 0.0),
                "price_open": getattr(p, "price_open", 0.0),
                "sl": getattr(p, "sl", 0.0),
                "tp": getattr(p, "tp", 0.0),
            })
        return out

    def _reconcile_history(self, symbol: str | None, magic: int) -> list[dict]:
        try:
            orders = self.call("history_orders_get",
                               self._mt5_or_raise().history_orders_get,
                               int(time.time()) - self.order_lookback,
                               int(time.time())) or []
        except Exception:
            # Older builds lack history_orders_get; treat as no evidence
            # rather than an error, so the position check governs.
            return []

        out = []
        for o in orders:
            if magic is not None and getattr(o, "magic", None) not in (magic, None):
                continue
            if symbol and getattr(o, "symbol", None) != symbol:
                continue
            out.append({
                "ticket": getattr(o, "ticket", None),
                "order_id": getattr(o, "order_id", None),
                "symbol": getattr(o, "symbol", symbol),
            })
        return out

    def _mt5_or_raise(self):
        """Resolve the MT5 module, or raise if it is unavailable.

        Every read helper must go through this. They used to dereference
        self._mt5 directly, which is None until _ensure_connected() has run on
        the owner thread. The result was
        `'NoneType' object has no attribute 'positions_get'` on the very first
        reconcile, so the engine never read broker state at all.
        """
        mt5 = self._resolve_mt5()
        if mt5 is None:
            raise GatewayError(
                "MetaTrader5 package is unavailable; the gateway cannot read "
                "or write terminal state"
            )
        return mt5

    # -- read helpers ----------------------------------------------------

    def account_info(self) -> Any:
        return self.call("account_info", self._mt5_or_raise().account_info)

    def symbol_info(self, symbol: str) -> Any:
        return self.call("symbol_info", self._mt5_or_raise().symbol_info, symbol)

    def symbol_info_tick(self, symbol: str) -> Any:
        return self.call("symbol_info_tick",
                         self._mt5_or_raise().symbol_info_tick, symbol)

    def symbol_select(self, symbol: str, select: bool = True) -> Any:
        return self.call("symbol_select",
                         self._mt5_or_raise().symbol_select, symbol, select)

    def positions_get(self, symbol: str | None = None,
                      ticket: int | None = None) -> list:
        """Open positions.

        MT5 has a quirk here: passing EITHER argument explicitly, even as None,
        returns None instead of the positions. `positions_get()` with no
        arguments returns everything; `positions_get(symbol=None)` returns
        None. The gateway always passed both, so every call came back empty and
        reconciliation silently saw zero exposure.

        Arguments are therefore only supplied when they actually carry a value.

        MT5 also returns None rather than an empty list for "no data", so None
        is normalised to [] and callers can iterate without a None check.
        """
        func = self._mt5_or_raise().positions_get
        if ticket is not None:
            result = self.call("positions_get", func, ticket=ticket)
        elif symbol is not None:
            result = self.call("positions_get", func, symbol=symbol)
        else:
            result = self.call("positions_get", func)
        return list(result) if result else []

    def close_position(self, ticket: int) -> OrderResult:
        """Close one position. Routed through send_order for reconciliation."""
        position = self.positions_get(ticket=ticket)
        if not position:
            return OrderResult(
                outcome=OrderOutcome.NOT_EXECUTED,
                resolution=Resolution.PROCEED,
                comment="position already gone",
            )
        pos = position[0]
        tick = self.symbol_info_tick(pos.symbol)
        price = tick.bid if pos.type == 0 else tick.ask

        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "position": ticket,
            "volume": pos.volume,
            "type": self._mt5.ORDER_TYPE_SELL if pos.type == 0
                    else self._mt5.ORDER_TYPE_BUY,
            "price": price,
            "deviation": 25,
            "magic": self.magic,
            "comment": "engine close",
        }
        return self.send_order(request)

    def shutdown_terminal(self) -> None:
        if self._mt5 is not None and self.status.connected:
            try:
                self._submit("shutdown", self._mt5.shutdown, timeout=2.0)
            except Exception:
                pass
        self.status.connected = False

    # -- helpers ---------------------------------------------------------

    def _retcode(self, name: str, default: int) -> int:
        return getattr(self._mt5, name, default)

    def _log(self, category: str, message: str, *, severity: str = "INFO",
             context: dict | None = None) -> None:
        if self.journal is not None:
            self.journal.log_event(category, message, severity=severity,
                                   source="gateway", context=context)

    @property
    def connected(self) -> bool:
        return self.status.connected


@dataclass
class _Request:
    """One queued call plus its completion signal."""

    name: str
    fn: Callable
    args: tuple
    kwargs: dict
    timeout: float
    done: threading.Event = field(default_factory=threading.Event)
    value: Any = None
    error: BaseException | None = None

    def resolve(self, value: Any) -> None:
        self.value = value
        self.done.set()

    def reject(self, error: BaseException) -> None:
        self.error = error
        self.done.set()

    def wait(self) -> Any:
        """Wait for the owner thread, or raise MT5Timeout.

        The deadline is enforced on the CALLER's wait. The owner runs the MT5
        call inline, so a blocked call blocks the owner and the caller must
        treat the outcome as unknown rather than assuming the call failed.
        """
        if not self.done.wait(timeout=self.timeout):
            raise MT5Timeout(self.name, self.timeout)
        if self.error is not None:
            raise self.error
        return self.value