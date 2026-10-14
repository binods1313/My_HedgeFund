"""A USD account must never render a euro sign.

The bug this guards against was real: `data_engine.py` returned a hardcoded
`"USD"` on the disconnected path, so an operator on a USD demo account saw a $
on an account nobody had read, and an operator on a EUR account would have seen
the same. Currency is only ever what `account_info().currency` says.

These tests assert the currency path from the broker value to the rendered
string, with no terminal involved.
Run: python -m pytest tests/test_ui/test_currency_rendering.py -v
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from formatting import currency_symbol, format_money


class TestCurrencyComesFromTheBroker:

    def test_a_usd_account_renders_a_dollar_sign(self):
        """The positive case: what the broker reports is what is shown."""
        account = SimpleNamespace(currency="USD", equity=100000.0)
        rendered = format_money(account.equity, currency=account.currency)
        assert "$" in rendered, f"a USD account must show $, got {rendered!r}"
        assert "€" not in rendered, "a USD account must never show a euro sign"

    def test_a_usd_account_never_renders_a_euro_sign(self):
        """The explicit regression: no euro on a USD account, anywhere."""
        account = SimpleNamespace(currency="USD", equity=1234.56)
        for value, currency in (
            (account.equity, account.currency),
            (1234.56, "USD"),
            (100000.0, "USD"),
        ):
            text = format_money(value, currency=currency)
            assert "€" not in text, (
                f"a USD account rendered a euro sign: {text!r}"
            )

    def test_a_eur_account_renders_a_euro_sign(self):
        """The control: the test can fail, so the pass above means something."""
        account = SimpleNamespace(currency="EUR", equity=1000.0)
        rendered = format_money(account.equity, currency=account.currency)
        assert "€" in rendered, f"a EUR account must show a euro, got {rendered!r}"
        assert "$" not in rendered

    @pytest.mark.parametrize("currency", ["USD", "EUR", "GBP", "JPY"])
    def test_each_account_gets_its_own_symbol(self, currency):
        symbol = currency_symbol(currency)
        others = {currency_symbol(other) for other in ("USD", "EUR", "GBP", "JPY")}
        others.discard(symbol)
        assert symbol
        assert symbol not in others, (
            f"{currency} renders the same symbol as another currency"
        )

    def test_an_unknown_currency_renders_no_symbol_rather_than_a_guess(self):
        """An unreported currency borrows no symbol and invents none.

        The observed contract is a bare number with no symbol and no code.
        That is the safe outcome: it never shows a *wrong* currency symbol, and
        a missing symbol is visible as missing rather than as a guess.
        """
        rendered = format_money(1234.56, currency="ZAR")
        assert rendered == "1,234.56", (
            f"an unlisted currency must render bare, got {rendered!r}"
        )
        for wrong in ("$", "€", "£", "¥"):
            assert wrong not in rendered, (
                f"an unlisted currency must not borrow {wrong!r}: {rendered!r}"
            )
        assert currency_symbol("ZAR") == "", (
            "an unlisted currency has no symbol; a default would be a guess"
        )

    def test_a_missing_currency_is_not_a_usd_default(self):
        """None must not silently become USD.

        This is the exact shape of the original bug: an unread account
        presented as a USD one.
        """
        rendered = format_money(1234.56, currency=None)
        assert "$" not in rendered, (
            "an unread currency must not render a dollar sign: "
            f"{rendered!r}"
        )


class TestNoHardcodedCurrencyInSource:
    """The display path must not contain a literal currency."""

    def test_no_literal_currency_fallback_in_the_display_modules(self):
        from formatting import __file__ as formatting_file

        source = open(formatting_file, encoding="utf-8").read()
        # The lookup table itself is legitimate; a hardcoded *default* is not.
        # So assert there is no "USD" used as a fallback argument.
        table_region = source.split("def currency_symbol", 1)[0]
        rest = source.split("def currency_symbol", 1)[1]
        for bad in ('currency="USD"', "currency='USD'", 'or "USD"',
                    "or 'USD'", '= "USD"', "= 'USD'"):
            assert bad not in rest, (
                f"{bad!r} would make USD a hardcoded fallback in {formatting_file}"
            )
        assert '"USD":' in table_region, (
            "the symbol lookup table should still map USD to $"
        )

    def test_the_engine_disconnected_path_has_no_currency(self):
        """The fixed line, asserted directly.

        `data_engine.get_account_telemetry()` returned `"currency": "USD"` when
        `account_info()` was None. That value is now None, and the UI renders
        an unknown currency rather than the wrong one.
        """
        import data_engine

        source = inspect.getsource(data_engine.get_account_telemetry)
        assert '"currency": "USD"' not in source, (
            "the disconnected path must not claim a currency"
        )
        assert '"currency": None' in source, (
            "the disconnected path must report an unknown currency"
        )
