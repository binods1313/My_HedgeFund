"""Readable copy for order types the engine does not accept."""

from __future__ import annotations

UNSUPPORTED = "Not available yet (market orders only)"

MARKET_HELP = "Fills now at the current price."
LIMIT_HELP = (
    "Buy below or sell above the market. Fills only at that price or better. "
    + UNSUPPORTED
)
CONDITIONAL_HELP = (
    "Activates when a trigger price is reached. " + UNSUPPORTED
)


def unsupported_order_message(order_type: str) -> str:
    """Empty for Market. Every other type stays disabled."""
    if str(order_type).strip().lower() == "market":
        return ""
    return UNSUPPORTED
