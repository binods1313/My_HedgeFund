"""Fake MetaTrader5 for engine gateway tests.

Models the behaviours the gateway must handle: a slow call that times out, an
order that executes despite the timeout, one that never executes, and a
reconciliation that itself fails.

No real terminal is involved. The module is injected into Gateway via
`mt5_module=`, so nothing imports MetaTrader5 here.
"""
from __future__ import annotations

import threading
import time


class FakeSymbolInfo:
    def __init__(self, name, path="Forex\\Majors", digits=5, point=0.00001,
                 volume_step=0.01, volume_min=0.01, volume_max=100.0,
                 trade_tick_value=1.0, trade_tick_size=0.00001):
        self.name = name
        self.path = path
        self.digits = digits
        self.point = point
        self.volume_step = volume_step
        self.volume_min = volume_min
        self.volume_max = volume_max
        self.trade_tick_value = trade_tick_value
        self.trade_tick_size = trade_tick_size
        self.description = ""
        self.visible = True
        self.filling_mode = 2


class FakeTick:
    def __init__(self, bid=1.10000, ask=1.10002, time=None):
        self.bid = bid
        self.ask = ask
        self.time = int(time if time is not None else time_module.time())


class FakePosition:
    def __init__(self, ticket, symbol="EURUSD", type_=0, volume=0.01,
                 price_open=1.10000, sl=0.0, tp=0.0, profit=0.0, magic=100100):
        self.ticket = ticket
        self.symbol = symbol
        self.type = type_
        self.volume = volume
        self.price_open = price_open
        self.price_current = price_open
        self.sl = sl
        self.tp = tp
        self.profit = profit
        self.magic = magic


class FakeOrder:
    def __init__(self, ticket, symbol="EURUSD", magic=100100, order_id=None):
        self.ticket = ticket
        self.symbol = symbol
        self.magic = magic
        self.order_id = order_id if order_id is not None else ticket


class FakeSendResult:
    def __init__(self, retcode=10009, order=555, comment="Done"):
        self.retcode = retcode
        self.order = order
        self.comment = comment
        self.deal = order


class FakeCheckResult:
    def __init__(self, retcode=0, comment="OK"):
        self.retcode = retcode
        self.comment = comment


class FakeAccount:
    #: The login and currency reported. The account guard compares the login
    #: against the configured MT5_LOGIN, so tests that build a real engine must
    #: install a matching credential -- see matching_fake_login() below.

    def __init__(self, trade_mode=0, login=999999999, currency="EUR",
                 server="FakeBroker-Demo"):
        self.trade_mode = trade_mode
        self.login = login
        self.server = "MetaQuotes-Demo"
        self.currency = currency
        self.balance = 100000.0
        self.equity = 100000.0
        self.profit = 0.0
        self.margin = 0.0
        self.margin_free = 100000.0
        self.margin_level = 0.0


#: The login FakeMT5's account reports. A test that builds a real engine must
#: configure the same login, or the account guard correctly refuses and the
#: test fails for a reason that has nothing to do with what it is testing.
FAKE_LOGIN = 999999999
FAKE_SERVER = "MetaQuotes-Demo"


def matching_fake_login(monkeypatch) -> None:
    """Configure MT5_LOGIN / MT5_SERVER to match FakeMT5's own account."""
    monkeypatch.setenv("MT5_LOGIN", str(FAKE_LOGIN))
    monkeypatch.setenv("MT5_SERVER", FAKE_SERVER)

# Behaviour switches, set per test.
BEHAVIOUR_NORMAL = "normal"
BEHAVIOUR_HANG = "hang"              # call blocks well past the deadline
BEHAVIOUR_TIMEOUT_THEN_EXECUTE = "timeout_then_execute"
BEHAVIOUR_TIMEOUT_NO_TRACE = "timeout_no_trace"
BEHAVIOUR_RECONCILE_FAILS = "reconcile_fails"
BEHAVIOUR_HANG_THEN_RECONCILE_FAILS = "hang_then_reconcile_fails"
BEHAVIOUR_RETURN_NONE = "return_none"
BEHAVIOUR_REJECT = "reject"          # definite broker rejection
BEHAVIOUR_INITIALIZE_FAILS = "init_fails"
BEHAVIOUR_LOGIN_FAILS = "login_fails"

import time as time_module  # noqa: E402  (placed after BEHAVIOUR_* for clarity)


class FakeMT5:
    """Configurable MT5 stand-in.

    Records every call so tests can assert on ordering and on whether a call was
    attempted at all.

    `strict_none_args` reproduces a real MT5 quirk discovered against the live
    terminal: positions_get() with NO arguments returns the positions, but
    passing either argument explicitly, even as None, returns None. The gateway
    always passed both, so it read zero positions while two were open.
    """

    #: Return None when positions_get is called with any explicit argument.
    STRICT_NONE_ARGS = "strict_none_args"

    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 2
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 2
    ORDER_TIME_GTC = 0
    ORDER_TIME_DAY = 1
    SYMBOL_TRADE_MODE_FULL = 4
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_REJECT = 10006
    TRADE_RETCODE_INVALID = 10013
    TIMEFRAME_M15 = 15
    TIMEFRAME_H4 = 16388
    TIMEFRAME_D1 = 16408
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2

    def __init__(self, behaviour=BEHAVIOUR_NORMAL, *,
                 positions=None, history=None, symbols=None,
                 trade_mode=0, fail_times=0):
        self.behaviour = behaviour
        self.calls: list[tuple[str, object]] = []
        self._positions = list(positions or [])
        self._history = list(history or [])
        self._symbols = symbols or {}
        self.account = FakeAccount(trade_mode=trade_mode)
        self.send_retcode = (self.TRADE_RETCODE_REJECT
                             if behaviour == BEHAVIOUR_REJECT
                             else self.TRADE_RETCODE_DONE)
        #: Number of upcoming initialise() calls that should fail.
        self.fail_times = fail_times
        self._init_calls = 0
        self._next_ticket = 700_000
        self.connected = False
        self._rates: dict[str, list[dict]] = {}
        #: When true, reproduce MT5 returning None if any argument is passed.
        self.strict_none_args = behaviour == self.STRICT_NONE_ARGS

    # -- helpers ---------------------------------------------------------

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def add_position(self, position: FakePosition) -> FakePosition:
        self._positions.append(position)
        return position

    def add_history(self, order: FakeOrder) -> FakeOrder:
        self._history.append(order)
        return order

    def add_rates(self, symbol: str, rates: list[dict]) -> None:
        """Install closed-bar data that ``copy_rates_from_pos`` will return."""
        self._rates[symbol] = list(rates)

    def _log(self, name, *args, **kwargs):
        # Real MT5 accepts keyword arguments such as terminal_path on
        # initialize(); record the call so it stays visible in the log.
        self.calls.append((name, args if len(args) > 1
                           else (args[0] if args else None)))

    # -- lifecycle -------------------------------------------------------

    def initialize(self, **kwargs):
        self._log("initialize", **kwargs)
        self._init_calls += 1
        if self.behaviour == BEHAVIOUR_INITIALIZE_FAILS and \
                self._init_calls <= max(1, self.fail_times):
            return False
        self.connected = True
        return True

    def login(self, login, password=None, server=None):
        self._log("login", login)
        if self.behaviour == BEHAVIOUR_LOGIN_FAILS:
            return False
        return True

    def shutdown(self):
        self._log("shutdown")
        self.connected = False
        return True

    def last_error(self):
        return (1, "fake error")

    def terminal_info(self):
        return type("T", (), {"connected": self.connected})()

    # -- market data -----------------------------------------------------

    def symbol_info(self, symbol):
        self._log("symbol_info", symbol)
        return self._symbols.get(symbol) or FakeSymbolInfo(symbol)

    def symbols_get(self, group=None):
        self._log("symbols_get", group)
        return list(self._symbols.values())

    def symbol_select(self, symbol, select=True):
        self._log("symbol_select", symbol, select)
        return True

    def symbol_info_tick(self, symbol):
        self._log("symbol_info_tick", symbol)
        return FakeTick()

    def account_info(self):
        self._log("account_info")
        return self.account

    # -- orders ----------------------------------------------------------

    def order_check(self, request):
        self._log("order_check", request)
        return FakeCheckResult()

    def order_send(self, request):
        self._log("order_send", request)

        if self.behaviour in (BEHAVIOUR_HANG,
                              BEHAVIOUR_HANG_THEN_RECONCILE_FAILS):
            # Blocks past any sane deadline. The owner thread is stuck, so the
            # terminal cannot be read for reconciliation either: the correct
            # outcome is UNKNOWN + HALT, not a guess.
            time_module.sleep(30)
            return FakeSendResult()

        if self.behaviour == BEHAVIOUR_TIMEOUT_THEN_EXECUTE:
            # The order IS processed, but the reply never arrives: order_send
            # returns None. This is the ghost-order case reconciliation exists
            # for, and testing it via a returned ambiguity rather than a hang
            # keeps the owner thread free to serve the reconciliation reads.
            self._next_ticket += 1
            self.add_position(FakePosition(
                ticket=self._next_ticket,
                symbol=request.get("symbol", "EURUSD"),
                magic=request.get("magic", 100100),
            ))
            return None

        if self.behaviour == BEHAVIOUR_TIMEOUT_NO_TRACE:
            # Processed nowhere: no position, no history, and the reply is lost.
            return None

        if self.behaviour == BEHAVIOUR_RETURN_NONE:
            return None

        if self.send_retcode != self.TRADE_RETCODE_DONE:
            return FakeSendResult(retcode=self.send_retcode, order=0,
                                  comment="rejected")

        self._next_ticket += 1
        ticket = self._next_ticket
        # A real broker reflects the fill immediately.
        if "position" not in request:
            self.add_position(FakePosition(
                ticket=ticket,
                symbol=request.get("symbol", "EURUSD"),
                type_=0 if request.get("type") == "BUY" else 1,
                volume=request.get("volume", 0.01),
                price_open=1.10000,
                magic=request.get("magic", 100100),
            ))
        return FakeSendResult(retcode=self.TRADE_RETCODE_DONE, order=ticket)

    def positions_get(self, *args, **kwargs):
        # Record whether the caller passed anything at all. MT5's behaviour
        # depends on the call being made with zero arguments versus one that
        # carries an explicit value or an explicit None.
        passed = bool(args or kwargs)
        symbol = kwargs.get("symbol", args[0] if len(args) > 0 else None)
        ticket = kwargs.get("ticket", args[1] if len(args) > 1 else None)
        self._log("positions_get", symbol if passed else "<omitted>",
                  ticket if passed else "<omitted>")

        if self.behaviour in (BEHAVIOUR_RECONCILE_FAILS,
                              BEHAVIOUR_HANG_THEN_RECONCILE_FAILS):
            raise RuntimeError("terminal disconnected mid-reconcile")

        if self.strict_none_args:
            # Measured against the live terminal:
            #   positions_get()                    -> all positions
            #   positions_get(symbol=None)         -> None
            #   positions_get(ticket=None)         -> None
            #   positions_get(symbol='USDCAD')     -> correctly filtered
            if not passed:
                return list(self._positions)
            if symbol is None and ticket is None:
                return None

        if ticket is not None:
            return [p for p in self._positions if p.ticket == ticket]
        if symbol is not None:
            return [p for p in self._positions if p.symbol == symbol]
        return list(self._positions)

    def history_orders_get(self, from_time, to_time=None):
        self._log("history_orders_get", from_time, to_time)
        if self.behaviour in (BEHAVIOUR_RECONCILE_FAILS,
                              BEHAVIOUR_HANG_THEN_RECONCILE_FAILS):
            raise RuntimeError("history unavailable")
        return list(self._history)

    def history_deals_get(self, *a, **kw):
        self._log("history_deals_get")
        return []

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        """Return ``count`` closed bars for ``symbol`` as a list of dicts.

        ``start_pos`` is treated as a slice offset: index 0 is the most
        recent bar. The production gateway uses start_pos=1 to skip the
        forming (not-yet-closed) bar at index 0, so tests inject a list
        whose index 0 is the forming bar and index 1+ are closed bars.
        """
        self._log("copy_rates_from_pos", symbol, timeframe, start_pos, count)
        series = self._rates.get(symbol) if hasattr(self, "_rates") else None
        if series is None:
            return None
        start = int(start_pos)
        end = start + int(count)
        return list(series[start:end])
