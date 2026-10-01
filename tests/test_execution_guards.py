"""Execution guard tests using a fake `mt5` module.

These tests prove the safety-critical ordering and gating rules of
`execution.execute_trade` without a live terminal:

  - order_check() runs BEFORE order_send(), and if order_check() fails,
    order_send() is never called at all.
  - Symbols outside the config.yaml whitelist, or that MT5 classifies as
    Unclassified, are rejected BEFORE any MT5 order call.
  - A lot below the broker's volume_min is rejected locally; order_send() is
    not called.
  - Orders without a stop loss are rejected.
  - get_available_symbols() never offers Unclassified or non-whitelisted
    symbols.

The fake records every call in order so assertions can be made on call
ordering, not merely on the returned dict.

Run: python -m pytest tests/test_execution_guards.py -v
"""
import sys
import types

import pytest

import execution
import data_engine


# ─────────────────────────────────────────────────────────────────────────────
# Fake MT5
# ─────────────────────────────────────────────────────────────────────────────

class FakeSymbolInfo:
    def __init__(self, name, path="Forex\\Majors", digits=5, point=0.00001,
                 volume_step=0.01, volume_min=0.01, volume_max=100.0,
                 filling_mode=2, visible=True, description=""):
        self.name = name
        self.path = path
        self.digits = digits
        self.point = point
        self.volume_step = volume_step
        self.volume_min = volume_min
        self.volume_max = volume_max
        self.filling_mode = filling_mode
        self.visible = visible
        self.description = description


class FakeTick:
    def __init__(self, bid, ask, time=1700000000):
        self.bid = bid
        self.ask = ask
        self.time = time


class FakeCheckResult:
    def __init__(self, retcode=0, comment="OK"):
        self.retcode = retcode
        self.comment = comment


class FakeSendResult:
    def __init__(self, retcode=10009, order=555, comment="Done"):
        self.retcode = retcode
        self.order = order
        self.comment = comment


class FakeMT5(types.ModuleType):
    """Minimal MT5 stand-in that logs every call in order."""

    # Constants used by execution.py
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 2
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 2
    ORDER_TIME_GTC = 0
    TRADE_RETCODE_DONE = 10009
    TIMEFRAME_M15 = 15
    TIMEFRAME_H4 = 16388
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2

    def __init__(self, symbols=None, ticks=None, check_retcode=0,
                 send_retcode=10009, trade_mode=None):
        super().__init__("MetaTrader5")
        self._symbols = symbols or {}
        self._ticks = ticks or {}
        self.check_retcode = check_retcode
        self.send_retcode = send_retcode
        # None means DEMO, so mode-guard tests can override explicitly.
        self.trade_mode = (self.ACCOUNT_TRADE_MODE_DEMO
                           if trade_mode is None else trade_mode)
        self.calls = []          # ordered log of (name, args)
        self.sent_requests = []
        self.connected = True

    # -- helpers used by tests --
    def call_names(self):
        return [name for name, _ in self.calls]

    def calls_to(self, name):
        return [args for n, args in self.calls if n == name]

    def _log(self, name, *args):
        self.calls.append((name, args))

    # -- MT5 API surface --
    def initialize(self, **kwargs):
        self._log("initialize", **kwargs)
        return True

    def shutdown(self):
        self._log("shutdown")

    def terminal_info(self):
        return types.SimpleNamespace(connected=self.connected)

    def symbol_info(self, symbol):
        self._log("symbol_info", symbol)
        return self._symbols.get(symbol)

    def symbols_get(self, group=None):
        self._log("symbols_get", group)
        return list(self._symbols.values())

    def symbol_select(self, symbol, select=True):
        self._log("symbol_select", symbol, select)
        return symbol in self._symbols

    def symbol_info_tick(self, symbol):
        self._log("symbol_info_tick", symbol)
        return self._ticks.get(symbol)

    def order_check(self, request):
        self._log("order_check", request)
        if self.check_retcode == "none":
            return None
        return FakeCheckResult(retcode=self.check_retcode)

    def order_send(self, request):
        self._log("order_send", request)
        self.sent_requests.append(request)
        return FakeSendResult(retcode=self.send_retcode)

    def last_error(self):
        return (1, "fake error")

    def positions_get(self, symbol=None, ticket=None):
        self._log("positions_get", symbol, ticket)
        return []

    def account_info(self):
        self._log("account_info")
        # DEMO by default: these tests exercise order mechanics, and the
        # account-mode guard must not mask what they are checking.
        return types.SimpleNamespace(
            trade_mode=self.ACCOUNT_TRADE_MODE_DEMO,
            login=999999999, server="MetaQuotes-Demo", currency="EUR",
            balance=10000.0, equity=10000.0, profit=0.0,
            margin=0.0, margin_free=10000.0, margin_level=0.0,
        )

    def copy_rates_from_pos(self, *a, **kw):
        self._log("copy_rates_from_pos", *a, **kw)
        return None


@pytest.fixture
def fake_mt5(monkeypatch):
    """Install a fake mt5 module into both execution and data_engine."""
    fake = FakeMT5(
        symbols={
            "EURUSD": FakeSymbolInfo("EURUSD", path="Forex\\Majors"),
            "XAUUSD": FakeSymbolInfo("XAUUSD", path="Metals\\Precious"),
            # Present on the broker and classifiable, but NOT in the config
            # whitelist: must be rejected by the whitelist gate.
            "AUDUSDX": FakeSymbolInfo("AUDUSDX", path="Forex\\Majors"),
            # Present on the broker and whitelisted in config, but its MT5 path
            # matches no asset class: must be rejected as Unclassified.
            "MYSTERY": FakeSymbolInfo("MYSTERY", path="Weird\\Unknown"),
        },
        ticks={
            "EURUSD": FakeTick(1.08500, 1.08502),
            "XAUUSD": FakeTick(2000.00, 2000.30),
            "AUDUSDX": FakeTick(0.65, 0.6501),
            "MYSTERY": FakeTick(10.0, 10.1),
        },
    )
    monkeypatch.setattr(execution, "mt5", fake)
    monkeypatch.setattr(data_engine, "mt5", fake)
    return fake


# ─────────────────────────────────────────────────────────────────────────────
# order_check / order_send ordering
# ─────────────────────────────────────────────────────────────────────────────

class TestOrderCheckRunsBeforeSend:

    def test_order_check_failure_never_calls_order_send(self, fake_mt5):
        """If order_check() fails, order_send() must not be called at all."""
        fake_mt5.check_retcode = 1  # a failing retcode

        result = execution.execute_trade("EURUSD", "BUY", lot_size=0.10, sl_pips=30)

        assert result["success"] is False
        assert result["status"] == "REJECTED"
        assert "Pre-flight check failed" in result["message"]
        assert "order_send" not in fake_mt5.call_names(), (
            f"order_send was called despite a failed pre-flight check: "
            f"{fake_mt5.call_names()}"
        )
        assert fake_mt5.sent_requests == []

    def test_order_check_returning_none_never_calls_order_send(self, fake_mt5):
        """order_check() returning None is also a failure, not a pass."""
        fake_mt5.check_retcode = "none"

        result = execution.execute_trade("EURUSD", "BUY", lot_size=0.10, sl_pips=30)

        assert result["success"] is False
        assert "order_send" not in fake_mt5.call_names()

    def test_order_check_is_called_before_order_send(self, fake_mt5):
        """Assert the ORDER of calls, not just that both happened."""
        fake_mt5.check_retcode = 0

        result = execution.execute_trade("EURUSD", "BUY", lot_size=0.10, sl_pips=30)

        assert result["success"] is True
        names = fake_mt5.call_names()
        assert "order_check" in names, names
        assert "order_send" in names, names
        assert names.index("order_check") < names.index("order_send"), (
            f"order_check must precede order_send; got {names}"
        )

    def test_successful_order_carries_sl(self, fake_mt5):
        """The broker-side SL must be present on the request we send."""
        execution.execute_trade("EURUSD", "BUY", lot_size=0.10, sl_pips=30)
        req = fake_mt5.sent_requests[0]
        assert req["sl"] > 0, "broker-side stop loss must be set"
        assert req["sl"] < req["price"], "BUY stop loss must sit below entry"
        assert req["magic"] == 100100

    def test_send_failure_retcode_is_reported(self, fake_mt5):
        fake_mt5.send_retcode = 10027
        result = execution.execute_trade("EURUSD", "BUY", lot_size=0.10, sl_pips=30)
        assert result["success"] is False
        assert "10027" in result["message"]


# ─────────────────────────────────────────────────────────────────────────────
# Symbol gating
# ─────────────────────────────────────────────────────────────────────────────

class TestSymbolWhitelistEnforcement:

    def test_non_whitelisted_symbol_is_rejected_before_any_order_call(self, fake_mt5):
        """AUDUSDX is on the broker but absent from config.yaml's whitelist."""
        result = execution.execute_trade("AUDUSDX", "BUY", lot_size=0.10, sl_pips=30)

        assert result["success"] is False
        assert result["status"] == "REJECTED"
        assert "tradable whitelist" in result["message"]

        names = fake_mt5.call_names()
        assert "order_check" not in names, names
        assert "order_send" not in names, names
        assert "symbol_info_tick" not in names, (
            f"gating must happen before market data is fetched; got {names}"
        )

    def test_unclassified_symbol_is_rejected_before_any_order_call(self, fake_mt5, monkeypatch):
        """A symbol MT5 cannot classify is rejected even if whitelisted.

        'MYSTERY' is whitelisted here, but its MT5 path matches no asset class,
        so it classifies as Unclassified and must not be traded.
        """
        fake_mt5._symbols["MYSTERY"] = FakeSymbolInfo("MYSTERY", path="Weird\\Unknown")
        fake_mt5._ticks["MYSTERY"] = FakeTick(10.0, 10.1)

        # Whitelist it, so only the classification check can reject it.
        monkeypatch.setattr(
            execution, "_load_tradable_whitelist",
            lambda: {"EURUSD", "XAUUSD", "MYSTERY"},
        )

        result = execution.execute_trade("MYSTERY", "BUY", lot_size=0.10, sl_pips=30)

        assert result["success"] is False
        assert "Unclassified" in result["message"]
        assert "order_send" not in fake_mt5.call_names()
        assert "order_check" not in fake_mt5.call_names()

    def test_whitelisted_and_classified_symbol_is_allowed(self, fake_mt5):
        result = execution.execute_trade("EURUSD", "BUY", lot_size=0.10, sl_pips=30)
        assert result["success"] is True

    def test_missing_symbol_is_rejected(self, fake_mt5):
        result = execution.execute_trade("NOSUCH", "BUY", lot_size=0.10, sl_pips=30)
        assert result["success"] is False
        assert "order_send" not in fake_mt5.call_names()


# ─────────────────────────────────────────────────────────────────────────────
# Lot sizing
# ─────────────────────────────────────────────────────────────────────────────

class TestLotGuards:

    def test_lot_below_volume_min_is_rejected_without_sending(self, fake_mt5):
        result = execution.execute_trade("EURUSD", "BUY", lot_size=0.001, sl_pips=30)

        assert result["success"] is False
        assert result["status"] == "REJECTED"
        assert "below broker minimum" in result["message"].lower()
        assert "order_send" not in fake_mt5.call_names()
        assert fake_mt5.sent_requests == []

    def test_float_edge_case_0_07_reaches_the_broker_unchanged(self, fake_mt5):
        """0.07 / 0.01 is 6.999... in float arithmetic; Decimal must give 0.07."""
        result = execution.execute_trade("EURUSD", "BUY", lot_size=0.07, sl_pips=30)
        assert result["success"] is True
        assert fake_mt5.sent_requests[0]["volume"] == pytest.approx(0.07)

    def test_step_rounding_floors(self, fake_mt5):
        execution.execute_trade("EURUSD", "BUY", lot_size=0.059, sl_pips=30)
        assert fake_mt5.sent_requests[0]["volume"] == pytest.approx(0.05)

    def test_volume_max_is_clamped(self, fake_mt5):
        execution.execute_trade("EURUSD", "BUY", lot_size=999.0, sl_pips=30)
        assert fake_mt5.sent_requests[0]["volume"] == pytest.approx(100.0)


# ─────────────────────────────────────────────────────────────────────────────
# Stop loss requirement
# ─────────────────────────────────────────────────────────────────────────────

class TestStopLossIsMandatory:

    @pytest.mark.parametrize("sl_pips", [0, 0.0, -10, None])
    def test_order_without_sl_is_rejected(self, fake_mt5, sl_pips):
        result = execution.execute_trade("EURUSD", "BUY", lot_size=0.10, sl_pips=sl_pips)

        assert result["success"] is False
        assert result["status"] == "REJECTED"
        assert "stop loss" in result["message"].lower()
        assert "order_send" not in fake_mt5.call_names()
        assert fake_mt5.sent_requests == []

    def test_sell_without_sl_is_rejected(self, fake_mt5):
        result = execution.execute_trade("EURUSD", "SELL", lot_size=0.10, sl_pips=0)
        assert result["success"] is False
        assert "order_send" not in fake_mt5.call_names()

    def test_sell_sl_sits_above_entry(self, fake_mt5):
        execution.execute_trade("EURUSD", "SELL", lot_size=0.10, sl_pips=30)
        req = fake_mt5.sent_requests[0]
        assert req["sl"] > req["price"], "SELL stop loss must sit above entry"


# ─────────────────────────────────────────────────────────────────────────────
# Dropdown source
# ─────────────────────────────────────────────────────────────────────────────

class TestDropdownNeverOffersUntradableSymbols:

    def _all_symbols(self, result):
        return [s for syms in result.values() for s in syms]

    def test_unclassified_is_never_offered(self, fake_mt5, monkeypatch):
        """MYSTERY is whitelisted in config but unclassifiable by MT5."""
        monkeypatch.setattr(
            execution, "_load_tradable_whitelist",
            lambda: {"EURUSD", "XAUUSD", "MYSTERY"},
        )
        result = data_engine.get_available_symbols()
        assert "MYSTERY" not in self._all_symbols(result)
        assert not any("Unclassified" in cat for cat in result), (
            f"Unclassified must not appear as a tradable category: {list(result)}"
        )

    def test_non_whitelisted_is_never_offered(self, fake_mt5):
        result = data_engine.get_available_symbols()
        assert "AUDUSDX" not in self._all_symbols(result)

    def test_whitelisted_symbols_are_offered(self, fake_mt5):
        result = data_engine.get_available_symbols()
        assert "EURUSD" in self._all_symbols(result)
        assert "XAUUSD" in self._all_symbols(result)

    def test_config_failure_yields_no_symbols(self, fake_mt5, monkeypatch):
        """Fails closed: if config is unreadable we offer nothing."""
        import config_loader
        monkeypatch.setattr(
            config_loader, "load_config",
            lambda *a, **kw: (_ for _ in ()).throw(ValueError("broken config")),
        )
        assert data_engine.get_available_symbols() == {}


# ─────────────────────────────────────────────────────────────────────────────
# Classification
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifySymbol:

    @pytest.mark.parametrize("path,expected", [
        ("Forex\\Majors", "Forex Currencies"),
        ("FX\\Majors", "Forex Currencies"),
        ("Crypto\\Bitcoin", "Crypto Assets"),
        ("Metals\\Precious", "Metals & Commodities"),
        ("Commodities\\Energy", "Metals & Commodities"),
        ("Indices\\US", "Indices & Equities"),
        ("Shares\\AETH", "Indices & Equities"),
        ("Weird\\Unknown", "Unclassified"),
        ("Something\\Unexpected", "Unclassified"),
        ("", "Unclassified"),
    ])
    def test_classification_by_path(self, path, expected):
        assert data_engine._classify_symbol(FakeSymbolInfo("X", path=path)) == expected

    def test_aeth_name_does_not_imply_crypto(self):
        """The original bug: substring 'ETH' in 'AETH' implied crypto.

        Classification is by MT5 path, never by substring-matching the name,
        so 'AETH' under an equity path is an equity, not crypto.
        """
        sym = FakeSymbolInfo("AETH", path="Shares\\ETF")
        assert data_engine._classify_symbol(sym) == "Indices & Equities"

    def test_aeth_under_unknown_path_is_unclassified(self):
        sym = FakeSymbolInfo("AETH", path="Broker\\Mystery")
        assert data_engine._classify_symbol(sym) == "Unclassified"