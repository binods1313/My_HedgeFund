"""Presentation helpers for account telemetry.

Pure formatting only. These live outside app.py so they can be unit-tested:
the HUD previously inlined this logic, which is how a stale hardcoded `$`
survived in one place while the currency was fixed in another.

Formatting rules live here, not in the view. config.yaml supplies the
thresholds so the safety-relevant cut-offs are changeable without touching UI
code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from config_loader import load_config

# Currency symbols for the account currency.
CURRENCY_SYMBOLS = {
    "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥",
    "CHF": "₣", "AUD": "A$", "CAD": "C$", "NZD": "NZ$",
}


def currency_symbol(currency: str | None) -> str:
    """Symbol for an account currency; empty string when unknown.

    An unknown currency falls back to no symbol rather than a wrong one: showing
    "$" for a EUR account is worse than showing a bare number.
    """
    if not currency:
        return ""
    return CURRENCY_SYMBOLS.get(str(currency).strip().upper(), "")


def format_money(amount: float | None, currency: str | None = None,
                 decimals: int = 2) -> str:
    """Format an account-currency amount with the correct symbol.

    The sign precedes the symbol ('-EUR13.42' rendered as '-€13.42'), which is
    the conventional ordering for European currencies.

    NaN and None render as 0.00 rather than 'nan', because a NaN reaching the
    HUD indicates a bad read and 'nan' is not a useful thing to display.
    """
    value = _finite_or_zero(amount)
    symbol = currency_symbol(currency)
    sign = "-" if value < 0 else ""
    return f"{sign}{symbol}{abs(value):,.{decimals}f}"


def _finite_or_zero(value) -> float:
    """Coerce to a finite float; None/NaN/inf/bad types become 0.0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    f = float(value)
    if math.isnan(f) or math.isinf(f):
        return 0.0
    return f


def _margin_threshold() -> float:
    """Minimum margin used, in account currency, for margin level to display.

    Read from config.yaml so the cut-off is configurable rather than a magic
    number spread across the UI.
    """
    try:
        return float(load_config().display.margin_level_na_below)
    except Exception:
        # A config failure must not silently change the safety display. Fall back
        # to the documented default rather than returning 0, which would make a
        # genuinely flat account look alarming.
        return 1.0


def margin_level_is_meaningful(margin_used, margin_level,
                               threshold: float | None = None) -> bool:
    """Is a margin-level percentage worth showing?

    Margin level is equity/margin. With no margin in use it is undefined and
    the broker reports 0, which some UIs render as a huge or infinite number.
    A margin level is therefore only meaningful once margin actually in use
    exceeds the threshold.

    Returns False for margin at or below the threshold, and for negative or
    non-finite margin.
    """
    used = _finite_or_zero(margin_used)
    level = _finite_or_zero(margin_level)
    if used <= 0:
        return False
    if threshold is None:
        threshold = _margin_threshold()
    if used < threshold:
        return False
    # A reported level of exactly 0 with real margin in use is not trustworthy.
    return level > 0


def format_margin_level(margin_used, margin_level, currency: str | None = None,
                        threshold: float | None = None) -> str:
    """Format margin level for display, or 'N/A' when it is not meaningful.

    Args:
        margin_used:  margin currently in use, in account currency.
        margin_level: the broker's reported margin level (percent).
        currency:    account currency, for the threshold unit and the symbol.
        threshold:   override for the minimum meaningful margin used;
                     defaults to display.margin_level_na_below in config.yaml.

    Returns a percentage string such as '487,498.5%', or 'N/A'.
    """
    if not margin_level_is_meaningful(margin_used, margin_level, threshold):
        return "N/A"
    level = _finite_or_zero(margin_level)
    return f"{level:,.1f}%"


def format_open_risk(amount: float | None, currency: str | None = None) -> str:
    """Format total open risk, always signed so the sign is unambiguous."""
    value = _finite_or_zero(amount)
    symbol = currency_symbol(currency)
    sign = "+" if value >= 0 else "-"
    return f"{sign}{symbol}{abs(value):,.2f}"


def mask_account(login) -> str:
    """Mask an account number to its last 3 digits."""
    text = str(login)
    if len(text) <= 3:
        return "*" * len(text)
    return "*" * (len(text) - 3) + text[-3:]