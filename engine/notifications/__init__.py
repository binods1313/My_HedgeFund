"""Notification layer for V2.0 AI Trading Terminal.

Submodules:
- telegram_service: Telegram approval layer

Event types are re-exported from engine.journal so the entire
telegram event taxonomy is visible through this package.
"""

from engine.journal import (
    TELEGRAM_EXECUTION_FAILED,
    TELEGRAM_SIGNAL_APPROVED,
    TELEGRAM_SIGNAL_FOUND,
    TELEGRAM_SIGNAL_REJECTED,
    TELEGRAM_SIGNAL_SENT_TO_TELEGRAM,
    TELEGRAM_TRADE_EXECUTED,
)
from engine.notifications.telegram_service import (
    PendingTrade,
    _bot,
    _chat_id,
    _pending_trades,
    _telegram_initialized,
    cleanup_pending_trades,
    get_pending_trade,
    handle_approval_message,
    init_telegram,
    pending_trade_count,
    poll_updates,
    process_no_approval,
    process_yes_approval,
    remove_pending_trade,
    send_approval_message,
    stop_telegram_polling,
    store_pending_trade,
)

__all__ = [
    # Journal event types
    "TELEGRAM_SIGNAL_FOUND",
    "TELEGRAM_SIGNAL_SENT_TO_TELEGRAM",
    "TELEGRAM_SIGNAL_APPROVED",
    "TELEGRAM_SIGNAL_REJECTED",
    "TELEGRAM_TRADE_EXECUTED",
    "TELEGRAM_EXECUTION_FAILED",
    # Trade management
    "PendingTrade",
    "_pending_trades",
    "_chat_id",
    "_bot",
    "_telegram_initialized",
    "cleanup_pending_trades",
    "get_pending_trade",
    "handle_approval_message",
    "init_telegram",
    "pending_trade_count",
    "poll_updates",
    "process_no_approval",
    "process_yes_approval",
    "remove_pending_trade",
    "send_approval_message",
    "stop_telegram_polling",
    "store_pending_trade",
]
