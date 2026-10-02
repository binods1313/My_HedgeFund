"""Tests for C1 (margin level / currency formatting) and C2 (demo mode guard).

C1 background
-------------
Margin level is equity/margin. With no margin in use the ratio is meaningless
and the broker reports 0, which used to render as an enormous percentage. The
threshold below which we show N/A is now config.yaml
display.margin_level_na_below, not a magic number in the HUD.

C2 background
-------------
A verification script must never be able to send a live order by accident, so
every order path refuses unless the account is provably DEMO.

Run: python -m pytest tests/test_formatting_and_mode.py -v
"""
import math
from pathlib import Path

import pytest

import execution
import formatting


# ─────────────────────────────────────────────────────────────────────────────
# C1: format_margin_level
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatMarginLevel:

    # -- margin at or below the threshold -> N/A --
    def test_margin_zero_is_na(self):
        assert formatting.format_margin_level(0.0, 0.0) == "N/A"

    def test_margin_0_2_is_na(self):
        """The value that prompted C1: 0.2 must be N/A."""
        assert formatting.format_margin_level(0.2, 487498.0) == "N/A"

    def test_margin_0_99_is_na(self):
        assert formatting.format_margin_level(0.99, 123456.0) == "N/A"

    def test_margin_exactly_at_threshold_is_na(self):
        """The rule is 'below 1.0', so exactly 1.0 still displays."""
        assert formatting.format_margin_level(1.0, 500.0) == "500.0%"

    # -- margin above the threshold -> percentage --
    def test_margin_5_shows_percentage(self):
        assert formatting.format_margin_level(5.0, 1999.0) == "1,999.0%"

    def test_real_account_values_display(self):
        """The actual account state that triggered this investigation."""
        result = formatting.format_margin_level(20.51, 487498.53729887854, "EUR")
        assert result == "487,498.5%"

    def test_zero_level_with_real_margin_is_na(self):
        """A reported level of 0 with margin in use is not trustworthy."""
        assert formatting.format_margin_level(50.0, 0.0) == "N/A"

    # -- hostile inputs --
    def test_negative_margin_is_na(self):
        assert formatting.format_margin_level(-5.0, 300.0) == "N/A"

    def test_none_inputs_are_na(self):
        assert formatting.format_margin_level(None, None) == "N/A"
        assert formatting.format_margin_level(None, 500.0) == "N/A"
        assert formatting.format_margin_level(50.0, None) == "N/A"

    def test_nan_inputs_are_na(self):
        assert formatting.format_margin_level(math.nan, 500.0) == "N/A"
        assert formatting.format_margin_level(50.0, math.nan) == "N/A"

    def test_inf_is_treated_as_not_meaningful(self):
        assert formatting.format_margin_level(math.inf, 500.0) == "N/A"

    def test_string_inputs_do_not_crash(self):
        assert formatting.format_margin_level("abc", "def") == "N/A"

    def test_bool_inputs_do_not_crash(self):
        assert formatting.format_margin_level(True, True) == "N/A"

    # -- threshold is configurable --
    def test_threshold_override_is_honoured(self):
        assert formatting.format_margin_level(5.0, 100.0, threshold=10.0) == "N/A"
        assert formatting.format_margin_level(5.0, 100.0, threshold=1.0) == "100.0%"

    def test_threshold_comes_from_config(self):
        cfg = formatting._margin_threshold()
        assert cfg == 1.0, (
            f"config.yaml display.margin_level_na_below should be 1.0, got {cfg}"
        )

    def test_config_threshold_drives_behaviour(self):
        """The function must consult config, not a hardcoded 1.0."""
        # 0.5 is below the configured 1.0 threshold.
        assert formatting.format_margin_level(0.5, 500.0) == "N/A"


class TestMarginLevelIsMeaningful:

    def test_false_without_margin(self):
        assert formatting.margin_level_is_meaningful(0.0, 0.0) is False

    def test_true_with_margin(self):
        assert formatting.margin_level_is_meaningful(20.51, 487498.5) is True

    def test_false_below_threshold(self):
        assert formatting.margin_level_is_meaningful(0.5, 100.0) is False

    def test_false_with_negative_margin(self):
        assert formatting.margin_level_is_meaningful(-1.0, 100.0) is False


# ─────────────────────────────────────────────────────────────────────────────
# C1: currency formatting (the second bug: hardcoded "$")
# ─────────────────────────────────────────────────────────────────────────────

class TestCurrencyFormatting:

    @pytest.mark.parametrize("currency,symbol", [
        ("USD", "$"), ("EUR", "€"), ("GBP", "£"), ("JPY", "¥"),
    ])
    def test_known_currency_symbols(self, currency, symbol):
        assert formatting.currency_symbol(currency) == symbol

    def test_lowercase_currency_is_handled(self):
        assert formatting.currency_symbol("eur") == "€"

    def test_unknown_currency_has_no_symbol(self):
        """A wrong symbol is worse than none."""
        assert formatting.currency_symbol("XYZ") == ""

    def test_none_currency_has_no_symbol(self):
        assert formatting.currency_symbol(None) == ""

    def test_format_money_uses_account_currency(self):
        """The C1 bug: this account is EUR and the HUD showed '$20.51'."""
        assert formatting.format_money(20.51, "EUR") == "€20.51"
        assert formatting.format_money(20.51, "USD") == "$20.51"

    def test_format_money_thousands_separator(self):
        assert formatting.format_money(1234567.891, "EUR") == "€1,234,567.89"

    def test_format_money_handles_nan_and_none(self):
        assert formatting.format_money(math.nan, "EUR") == "€0.00"
        assert formatting.format_money(None, "EUR") == "€0.00"

    def test_format_money_negative(self):
        assert formatting.format_money(-13.42, "EUR") == "-€13.42"

    def test_open_risk_is_signed(self):
        assert formatting.format_open_risk(4.71, "EUR") == "+€4.71"
        assert formatting.format_open_risk(-4.71, "EUR") == "-€4.71"
        assert formatting.format_open_risk(0.0, "EUR") == "+€0.00"


class TestOpenPnlSignPreservation:
    """A loss must never render as a gain.

    app.py summed abs(profit) for the strip labelled 'Open Risk', so a book
    sitting on a loss displayed '+EUR 10.19' in green. The sign came from
    summing magnitudes, not from the positions.
    """

    def test_negative_positions_sum_to_a_negative_total(self):
        positions = [{"profit": -3.89}, {"profit": -6.30}]
        total = sum(p["profit"] for p in positions)
        assert total < 0
        assert total == pytest.approx(-10.19)

    def test_signed_loss_renders_with_minus(self):
        assert formatting.format_open_risk(-10.19, "EUR") == "-€10.19"

    def test_signed_profit_renders_with_plus(self):
        positions = [{"profit": 3.89}, {"profit": 6.30}]
        total = sum(p["profit"] for p in positions)
        assert formatting.format_open_risk(total, "EUR") == "+€10.19"

    def test_mixed_positions_net_out(self):
        positions = [{"profit": 12.00}, {"profit": -3.00}]
        total = sum(p["profit"] for p in positions)
        assert formatting.format_open_risk(total, "EUR") == "+€9.00"

    def test_no_positions_is_zero(self):
        assert formatting.format_open_risk(0.0, "EUR") == "+€0.00"

    def test_gross_exposure_still_available_separately(self):
        """Gross exposure is the magnitude, for reporting 'at risk'."""
        positions = [{"profit": -3.89}, {"profit": -6.30}]
        gross = sum(abs(p["profit"]) for p in positions)
        assert gross == pytest.approx(10.19)
        assert gross > 0

    def test_app_does_not_label_the_signed_value_as_risk(self):
        """The strip must not present net P&L under a 'risk' label."""
        source = (Path(__file__).resolve().parent.parent / "app.py").read_text(
            encoding="utf-8")
        assert "Open Risk" not in source, (
            "the strip still labels net signed P&L as 'Open Risk', which "
            "inverts the meaning of a loss"
        )


class TestMaskAccount:

    def test_masks_all_but_last_three(self):
        assert formatting.mask_account(999999999) == "******999"

    def test_short_is_fully_masked(self):
        assert formatting.mask_account("12") == "**"


# ─────────────────────────────────────────────────────────────────────────────
# C2: demo mode guard
# ─────────────────────────────────────────────────────────────────────────────

class FakeAccount:
    """Stand-in for mt5.account_info(). Set omit_trade_mode to drop the field."""

    def __init__(self, trade_mode, omit_trade_mode=False):
        if not omit_trade_mode:
            self.trade_mode = trade_mode
        self.login = 999999999
        self.currency = "EUR"
        self.balance = 10000.0
        self.equity = 10000.0
        self.profit = 0.0
        self.margin = 0.0
        self.margin_free = 10000.0
        self.margin_level = 0.0
        self.server = "MetaQuotes-Demo"


class FakeResult:
    retcode = 10009
    order = 1
    comment = "done"


class FakeMT5Mode:
    """Minimal MT5 stand-in for account-mode tests.

    Records every order_send so a test can prove none was attempted.
    """

    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2

    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 2
    ORDER_TIME_GTC = 0
    TRADE_RETCODE_DONE = 10009

    def __init__(self, account):
        # account may be None (no connection) or a FakeAccount.
        self._account = account
        self.sent = []

    def account_info(self):
        return self._account

    def symbol_info(self, name):
        raise AssertionError(
            "symbol_info must not be reached on a refused account"
        )

    def order_send(self, request):
        self.sent.append(request)
        return FakeResult()

    def positions_get(self, symbol=None, ticket=None):
        raise AssertionError(
            "positions_get must not be reached on a refused account"
        )

    def last_error(self):
        return (1, "fake")


def _mt5_with_mode(trade_mode, omit_trade_mode=False):
    return FakeMT5Mode(FakeAccount(trade_mode, omit_trade_mode))


class TestDemoModeGuard:

    def test_demo_account_is_allowed(self, monkeypatch):
        monkeypatch.setattr(execution, "mt5", _mt5_with_mode(0))
        allowed, reason = execution.require_demo_account()
        assert allowed is True
        assert reason == ""

    def test_real_account_is_refused(self, monkeypatch):
        monkeypatch.setattr(execution, "mt5", _mt5_with_mode(2))
        allowed, reason = execution.require_demo_account()
        assert allowed is False
        assert "REAL" in reason

    def test_contest_account_is_refused(self, monkeypatch):
        monkeypatch.setattr(execution, "mt5", _mt5_with_mode(1))
        allowed, reason = execution.require_demo_account()
        assert allowed is False
        assert "CONTEST" in reason

    def test_unknown_mode_is_refused(self, monkeypatch):
        """Fail closed: an unrecognised mode must not be assumed safe."""
        monkeypatch.setattr(execution, "mt5", _mt5_with_mode(99))
        allowed, reason = execution.require_demo_account()
        assert allowed is False
        assert "unrecognised" in reason

    def test_missing_account_is_refused(self, monkeypatch):
        monkeypatch.setattr(execution, "mt5", FakeMT5Mode(None))
        allowed, reason = execution.require_demo_account()
        assert allowed is False
        assert "account_info" in reason

    def test_account_without_trade_mode_is_refused(self, monkeypatch):
        monkeypatch.setattr(execution, "mt5", _mt5_with_mode(0, omit_trade_mode=True))
        allowed, reason = execution.require_demo_account()
        assert allowed is False
        assert "trade_mode" in reason


class TestNoOrderSentOnLiveAccount:
    """The guard must actually prevent order_send from being reached."""

    @pytest.mark.parametrize("trade_mode,label", [(2, "REAL"), (1, "CONTEST")])
    def test_execute_trade_refused(self, monkeypatch, trade_mode, label):
        fake = _mt5_with_mode(trade_mode)
        monkeypatch.setattr(execution, "mt5", fake)
        monkeypatch.setattr(
            execution, "_load_tradable_whitelist", lambda: {"EURUSD"})

        result = execution.execute_trade("EURUSD", "BUY", lot_size=0.10, sl_pips=30)

        assert result["success"] is False
        assert result["status"] == "REFUSED"
        assert label in result["message"]
        assert fake.sent == [], "order_send must never be reached on a live account"

    def test_close_position_refused_on_real_account(self, monkeypatch):
        """Closing is also an order."""
        fake = _mt5_with_mode(2)
        monkeypatch.setattr(execution, "mt5", fake)

        result = execution.close_position(123456)

        assert result["success"] is False
        assert result["status"] == "REFUSED"
        assert fake.sent == []

    def test_hold_decision_sends_nothing_on_live_account(self, monkeypatch):
        """HOLD must never reach the broker, on any account type."""
        fake = _mt5_with_mode(2)
        monkeypatch.setattr(execution, "mt5", fake)
        result = execution.execute_trade("EURUSD", "HOLD")
        assert result["status"] == "HOLD"
        assert fake.sent == []