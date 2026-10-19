"""Telegram approval service for V2.0 AI Trading Terminal.

Responsibilities:
- Telegram initialization and authorization
- Message delivery with MarkdownV2 escaping
- Approval/Rejection handling (YES/NO commands)
- Duplicate protection for trade IDs
- Background polling lifecycle
- Journaling telegram events
- Shutdown cleanup

This module integrates with the existing V2 architecture:
  Strategies -> Confidence Engine -> Risk Engine -> LLM Validator -> Telegram
  Approval -> Executor
The LLM remains validation-only and never becomes the trade decision maker.
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Callable, Awaitable

import requests

from engine.journal import Journal, utc_now_iso
from engine.journal import (
    TELEGRAM_SIGNAL_FOUND,
    TELEGRAM_SIGNAL_REJECTED,
    TELEGRAM_SIGNAL_SENT_TO_TELEGRAM,
    TELEGRAM_TRADE_CANCELLED,
    TELEGRAM_TRADE_EXECUTED,
    TELEGRAM_EXECUTION_FAILED,
    TELEGRAM_SIGNAL_APPROVED,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PendingTrade dataclass
# ---------------------------------------------------------------------------

@dataclass
class PendingTrade:
    """Trade pending Telegram approval.

    Only one execution per ``trade_id`` is allowed. Later arrivals of the
    same ``trade_id`` are rejected with "Trade already processed."
    """

    trade_id: str
    symbol: str
    direction: str
    entry: float
    stop_loss: float
    take_profit: float
    lot_size: float
    confidence: float
    rationale: str
    created_at: datetime = field(default_factory=datetime.now)

    # internal tracking
    _processed: bool = field(default=False, init=False, repr=False)

    def mark_processed(self) -> None:
        self._processed = True

    @property
    def processed(self) -> bool:
        return self._processed


# ---------------------------------------------------------------------------
# Global state (managed by the engine)
# ---------------------------------------------------------------------------

_pending_trades: Dict[str, PendingTrade] = {}
_telegram_initialized = False
_bot = None
_chat_id = 0
_poll_interval = 3.0  # seconds between polling checks


# ---------------------------------------------------------------------------
# Mini bot client (replaces python-telegram-bot).
# ---------------------------------------------------------------------------


class _TelegramBot:
    """Thin HTTP client against the Telegram Bot API.

    Provides the same two calls the rest of the code uses:
    send_message and get_updates, so public function names stay unchanged.
    """

    def __init__(self, token: str) -> None:
        self._token = token
        self._base = f"https://api.telegram.org/bot{token}"

    def send_message(self, chat_id: int, text: str,
                     parse_mode: str = "MarkdownV2") -> dict:
        resp = requests.post(
            f"{self._base}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def get_updates(self, offset: int = 0, timeout: int = 10) -> list:
        resp = requests.get(
            f"{self._base}/getUpdates",
            params={"offset": offset, "timeout": timeout},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("result", [])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _markdown_v2_escape(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    escape_chars = r"_*[]()~`>#+-=|{}.!"
    return re.sub(f"[{re.escape(escape_chars)}]", r"\\\g<0>", text)


def _build_approval_message(trade: PendingTrade) -> str:
    """Build the MarkdownV2 approval message for a trade."""

    rationale_preview = trade.rationale[:80] + ("..." if len(trade.rationale) > 80 else "")

    msg = (f"🚀 Trade Setup Found\n\n"
           f"Symbol: {trade.symbol}\n"
           f"Direction: {trade.direction}\n"
           f"Entry: {trade.entry:.2f}\n"
           f"SL: {trade.stop_loss:.2f}\n"
           f"TP: {trade.take_profit:.2f}\n"
           f"Confidence: {trade.confidence:.0f}%\n\n"
           f"Rationale:\n{rationale_preview}\n\n"
           f"Trade ID: {trade.trade_id}\n\n"
           f"Reply:\nYES {trade.trade_id}\nto execute.\n\n"
           f"Reply:\nNO {trade.trade_id}\nto cancel.")

    return _markdown_v2_escape(msg)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

async def init_telegram(journal: Journal | None = None) -> bool:
    """Initialize the Telegram bot from environment variables.

    Reads: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    Fails gracefully if any required variable is missing - the engine will
    start without Telegram capability but will not crash.

    Returns True if initialized successfully, False otherwise.
    """

    global _bot, _chat_id, _telegram_initialized

    if _telegram_initialized:
        return True

    try:
        from engine.config import read_mt5_credentials
        # config_loader is not a direct import path; fall back to env vars below
    except Exception:  # noqa: BLE001
        pass

    # Read from env vars directly (dotenv is loaded at engine startup)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id_str = os.environ.get("TELEGRAM_CHAT_ID")

    # Validate
    missing = []
    if not token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not chat_id_str:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        msg = "Telegram init skipped - missing: " + ", ".join(missing)
        logger.warning(msg)
        if journal is not None:
            journal.log_event("telegram", msg, severity="WARNING", source="telegram")
        return False

    try:
        _chat_id = int(chat_id_str)
    except ValueError:
        error_msg = f"TELEGRAM_CHAT_ID must be an integer, got: {chat_id_str}"
        logger.error(error_msg)
        if journal is not None:
            journal.log_event("telegram", error_msg, severity="ERROR", source="telegram")
        return False

        _bot = _TelegramBot(token)
    _telegram_initialized = True

    logger.info("Telegram initialized - chat_id=%s", _chat_id)
    if journal is not None:
        journal.log_event("telegram", "Telegram approval layer initialized",
                          source="telegram", context={"chat_id": _chat_id})
    return True


# ---------------------------------------------------------------------------
# Pending trade management
# ---------------------------------------------------------------------------

def store_pending_trade(trade: PendingTrade, journal: Journal | None = None) -> bool:
    """Store a pending trade.

    Returns True if the trade was stored (new trade_id), False if a trade with
    the same trade_id already exists (duplicate protection).
    """

    if trade.trade_id in _pending_trades:
        logger.warning("Duplicate pending trade ignored: %s", trade.trade_id)
        if journal is not None:
            journal.log_event("telegram",
                              "Duplicate pending trade ignored",
                              severity="WARNING", source="telegram",
                              context={"trade_id": trade.trade_id})
        return False

    _pending_trades[trade.trade_id] = trade
    logger.info("Stored pending trade: %s (%s)", trade.trade_id, trade.symbol)
    if journal is not None:
        journal.log_event("telegram", "SIGNAL_FOUND", severity="INFO", source="telegram",
                          context={"trade_id": trade.trade_id, "symbol": trade.symbol,
                                   "direction": trade.direction})
    return True


def get_pending_trade(trade_id: str) -> Optional[PendingTrade]:
    """Retrieve a pending trade by trade_id, or None if not found."""

    return _pending_trades.get(trade_id)


def remove_pending_trade(trade_id: str, journal: Journal | None = None) -> bool:
    """Remove a pending trade from the in-memory cache.

    Returns True if removed, False if not found.
    """

    if trade_id not in _pending_trades:
        return False

    del _pending_trades[trade_id]
    logger.info("Removed pending trade: %s", trade_id)
    if journal is not None:
        journal.log_event("telegram", "Pending trade removed", source="telegram",
                          context={"trade_id": trade_id})
    return True


def pending_trade_count() -> int:
    """Return the number of pending trades."""

    return len(_pending_trades)


# ---------------------------------------------------------------------------
# Message delivery
# ---------------------------------------------------------------------------

async def send_approval_message(trade: PendingTrade,
                                journal: Journal | None = None) -> bool:
    """Send the approval message for trade to the authorized chat.

    Returns True if the message was sent, False if Telegram is not
    initialized or the send failed.
    """

    if not _telegram_initialized or _bot is None:
        logger.warning("Telegram not initialized - cannot send approval message")
        return False

    try:
        message = _build_approval_message(trade)
        await _bot.send_message(_chat_id, message, parse_mode="MarkdownV2")
        logger.info("Sent Telegram approval message for trade %s", trade.trade_id)
        if journal is not None:
            journal.log_event("telegram", "SIGNAL_SENT_TO_TELEGRAM", severity="INFO",
                              source="telegram",
                              context={"trade_id": trade.trade_id, "symbol": trade.symbol})
        return True
    except Exception as exc:  # pylint: disable=broad-except
        error_msg = f"Failed to send Telegram message for trade {trade.trade_id}: {exc}"
        logger.error(error_msg)
        if journal is not None:
            journal.log_event("telegram", "SIGNAL_FAILED_TO_SEND", severity="ERROR",
                              source="telegram",
                              context={"trade_id": trade.trade_id, "error": str(exc)})
        return False


# ---------------------------------------------------------------------------
# Approval / Rejection handlers
# ---------------------------------------------------------------------------

async def handle_approval_message(text: str, from_chat_id: int) -> str:
    """Process an incoming Telegram message.

    Looks for patterns ``YES <trade_id>`` or ``NO <trade_id>``.

    Returns one of:
      - "approved"  - user approved the trade
      - "rejected"  - user rejected the trade
      - "unauthorized" - chat not authorized
      - "unknown"  - message did not match expected format

    The caller must verify the trade exists and is not already processed
    before calling the execution logic.
    """

    global _chat_id

    # Authorization check
    if from_chat_id != _chat_id:
        logger.warning("Unauthorized chat attempted Telegram control: %s", from_chat_id)
        return "unauthorized"

    text = text.strip()

    # Match YES/NO/APPROVE/REJECT followed by the decision id.
    match = re.match(r"^(YES|NO|APPROVE|REJECT)\s+(\S+)$", text)
    if not match:
        return "unknown"

    command = match.group(1)
    if command in ("YES", "APPROVE"):
        return "approved"
    if command in ("NO", "REJECT"):
        return "rejected"

    return "unknown"


# ---------------------------------------------------------------------------
# Execution forwarding (Telegram does NOT execute directly)
# ---------------------------------------------------------------------------

async def process_yes_approval(trade_id: str, executor, journal: Journal | None = None,
                               gateway=None) -> str:
    """Process a YES approval.

    Verifies the trade exists and is not already processed, forwards the
    trade to the executor, and journals the result.

    Returns one of:
      - "executed"     - order was submitted successfully
      - "not_found"    - no such pending trade
      - "already_processed" - trade was already executed/cancelled
      - "executor_error" - execution failed or returned an error reason
    """

    global _pending_trades

    # 1. Verify trade exists
    trade = _pending_trades.get(trade_id)
    if trade is None:
        return "not_found"

    # 2. Verify not already processed
    if trade.processed:
        return "already_processed"

    # 3. Acquire execution lock (single execution per trade)
    trade.mark_processed()

    # 4. Forward to executor
    # Build an OrderRequest-like structure for the executor
    try:
        from engine.executor import (
            OrderRequest, RiskApproval, ExecutionResult,
            MODE_LIVE, MODE_PAPER,
        )

        risk_approved = RiskApproval(approved=True, decision_id=f"telegram_approved:{trade_id}")

        request = OrderRequest(
            symbol=trade.symbol.upper(),
            side=trade.direction.upper(),
            volume=trade.lot_size,
            sl=trade.stop_loss,
            tp=trade.take_profit,
            comment=f"telegram:{trade_id}",
        )

        result: ExecutionResult = executor.submit(request, risk_approved)
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Executor submission failed for trade %s: %s", trade_id, exc)
        if journal is not None:
            journal.log_event("telegram", "EXECUTION_FAILED", severity="ERROR",
                              source="telegram",
                              context={"trade_id": trade_id, "error": str(exc)})
        return "executor_error"

    # 6. Mark trade as processed (already done via mark_processed)
    # Keep the trade in the cache so a duplicate YES returns "already_processed".

    # 8. Journal the outcome
    if result.ok:
        if journal is not None:
            journal.log_event("telegram", "TRADE_EXECUTED", severity="INFO",
                              source="telegram",
                              context={"trade_id": trade_id, "symbol": trade.symbol,
                                       "ticket": result.ticket, "price": result.price})
        return "executed"
    logger.warning("Executor rejected trade %s: %s", trade_id, result.message)
    if journal is not None:
        journal.log_event("telegram", "TRADE_EXECUTED_FAILED", severity="ERROR",
                          source="telegram",
                          context={"trade_id": trade_id, "reason": result.message})
    return "executor_error"


async def process_no_approval(trade_id: str, journal: Journal | None = None) -> str:
    """Process a NO cancellation.

    Returns one of:
      - "cancelled"    - trade was cancelled and removed
      - "not_found"    - no such pending trade
    """

    # 1. Verify trade exists
    trade = get_pending_trade(trade_id)
    if trade is None:
        return "not_found"

    # 1. Remove pending trade
    remove_pending_trade(trade_id, journal)

    # 2. Mark cancelled
    logger.info("Trade %s cancelled by user", trade_id)
    if journal is not None:
        journal.log_event("telegram", "SIGNAL_REJECTED", severity="WARNING",
                          source="telegram",
                          context={"trade_id": trade_id, "symbol": trade.symbol,
                                   "reason": "user rejection"})

    # 3. Journal the cancellation
    if journal is not None:
        journal.log_event("telegram", "TRADE_CANCELLED", severity="INFO",
                          source="telegram",
                          context={"trade_id": trade_id, "symbol": trade.symbol})

    return "cancelled"


# ---------------------------------------------------------------------------
# Background polling (for webhook-less deployments)
# ---------------------------------------------------------------------------

async def poll_updates(stop_event: asyncio.Event,
                       update_handler: Callable[[str, int], Awaitable[str]]) -> None:
    """Long-poll Telegram updates until stop_event is set.

    update_handler is called with (text, from_chat_id) for each new message
    received and returns a short status string.

    This is a simple polling-based approach using the Telegram Bot API
    getUpdates. For production deployments, consider using a webhook.
    """

    if _bot is None:
        return

    while not stop_event.is_set():
        try:
            # Get updates via get_updates (python-telegram-bot 20+ API)
            updates = _bot.get_updates(offset=0, timeout=10)

            for update in updates:
                # Get the chat id and message text
                message = update.get("message")
                if message is None:
                    continue
                chat_id = (
                    message.get("from", {}).get("id")
                    if message.get("from")
                    else None
                )
                text = message.get("text")

                if chat_id and text:
                    asyncio.create_task(update_handler(text, chat_id))

            await asyncio.sleep(_poll_interval)

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Error in Telegram poll loop: %s", exc)
            await asyncio.sleep(_poll_interval)


# ---------------------------------------------------------------------------
# Shutdown cleanup
# ---------------------------------------------------------------------------

def cleanup_pending_trades() -> None:
    """Clear the pending-trade cache on shutdown.

    Removes all cached trades so no orphaned approvals can be processed
    after the engine stops.
    """

    global _pending_trades
    count = len(_pending_trades)
    _pending_trades.clear()
    logger.info("Cleared %s pending trades on shutdown", count)


def stop_telegram_polling(stop_event: asyncio.Event) -> None:
    """Signal the polling loop to stop.

    This is a cooperative shutdown: the poll loop checks stop_event between
    polling rounds and exits cleanly when it becomes set.
    """

    stop_event.set()
    logger.info("Telegram polling stop signaled")
