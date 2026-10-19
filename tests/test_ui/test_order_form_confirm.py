"""Confirm-order must call inspect_order with its real parameter names."""
from __future__ import annotations

import inspect

from ui.components.order_form import _confirm_order_dialog
from ui.components.trading import inspect_order


def test_confirm_passes_order_token_not_submit_token() -> None:
    """A wrong kwarg raises TypeError and the PAPER BUY never reaches submit_order."""
    call = inspect.getsource(_confirm_order_dialog)
    params = inspect.signature(inspect_order).parameters
    assert "order_token=pending[\"token\"]" in call or "order_token=pending['token']" in call
    assert "submit_token=" not in call
    assert "order_token" in params
    assert "submit_token" not in params
