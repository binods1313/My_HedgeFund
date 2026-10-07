"""Readable copy for order types the engine does not accept."""

from __future__ import annotations

UNSUPPORTED = "engine does not support this yet"

MARKET_HELP = "fills now at the current price"
LIMIT_HELP = "buy below / sell above market, fills only at that price or better"
CONDITIONAL_HELP = "activates when a trigger price is reached"


def unsupported_order_message(order_type: str) -> str:
    """Empty for Market. Every other type stays disabled."""
    if str(order_type).strip().lower() == "market":
        return ""
    return UNSUPPORTED
