"""Tests for engine/notifications/telegram_service.py.

All network calls are mocked. No real Telegram API calls are made.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from engine.notifications.telegram_service import (
    PendingTrade,
    cleanup_pending_trades,
    get_pending_trade,
    handle_approval_message,
    init_telegram,
    pending_trade_count,
    process_no_approval,
    process_yes_approval,
    remove_pending_trade,
    store_pending_trade,
    _markdown_v2_escape,
    _build_approval_message,
    _TelegramBot,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_telegram_state(monkeypatch):
    """Reset module-level globals before each test."""
    import engine.notifications.telegram_service as svc
    monkeypatch.setattr(svc, "_pending_trades", {})
    monkeypatch.setattr(svc, "_telegram_initialized", False)
    monkeypatch.setattr(svc, "_bot", None)
    monkeypatch.setattr(svc, "_chat_id", 0)
    yield


# ---------------------------------------------------------------------------
# 1. No MetaTrader5 import
# ---------------------------------------------------------------------------

def test_no_metatrader5_import():
    source = pathlib.Path(__file__).resolve().parents[2] / "engine" / "notifications" / "telegram_service.py"
    tree = compile(source.read_text(encoding="utf-8"), source, "exec", ast.PyCF_ONLY_AST)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "MetaTrader5" not in alias.name
        if isinstance(node, ast.ImportFrom):
            if node.module and "MetaTrader5" in node.module:
                pytest.fail("telegram_service.py imports MetaTrader5")


# ---------------------------------------------------------------------------
# 2. MarkdownV2 escaping — individual characters
# ---------------------------------------------------------------------------

SPECIAL_CHARS = r"_*[]()~`>#+-=|.!"

@pytest.mark.parametrize("char", list(SPECIAL_CHARS))
def test_markdown_v2_escapes_special_characters(char):
    escaped = _markdown_v2_escape(char)
    assert escaped == f"\\{char}", f"Expected \\{char!r}, got {escaped!r}"


def test_markdown_v2_leaves_plain_text_alone():
    assert _markdown_v2_escape("Hello World") == "Hello World"


# ---------------------------------------------------------------------------
# 3. MarkdownV2 escaping — full message contains escaped chars that appear in it
# ---------------------------------------------------------------------------

def test_build_approval_message_escapes_chars_that_appear():
    trade = PendingTrade(
        trade_id="T1",
        symbol="EURUSD",
        direction="LONG",
        entry=1.1000,
        stop_loss=1.0950,
        take_profit=1.1100,
        lot_size=0.1,
        confidence=85.0,
        rationale="RSI oversold*and MACD cross[test]_plus",
    )
    msg = _build_approval_message(trade)
    for char in "_*[]()~`>#+-=|.!":
        if char in "RSI oversold*and MACD cross[test]_plusEURUSD\nYES T1\nto execute.\nNO T1\nto cancel.":
            assert f"\\{char}" in msg, f"Missing escape for {char!r} in message: {msg}"


# ---------------------------------------------------------------------------
# 4. Unauthorized chat is ignored
# ---------------------------------------------------------------------------

def test_handle_approval_message_rejects_unauthorized_chat():
    import engine.notifications.telegram_service as svc
    svc._chat_id = 12345
    assert asyncio.run(handle_approval_message("YES T1", 99999)) == "unauthorized"
    assert asyncio.run(handle_approval_message("NO T1", 99999)) == "unauthorized"


# ---------------------------------------------------------------------------
# 5. YES executes exactly once, duplicate returns already_processed
# ---------------------------------------------------------------------------

def test_yes_executes_once_duplicate_returns_already_processed(monkeypatch):
    import engine.notifications.telegram_service as svc

    monkeypatch.setattr(svc, "_telegram_initialized", True)
    monkeypatch.setattr(svc, "_bot", object())
    monkeypatch.setattr(svc, "_chat_id", 12345)

    trade = PendingTrade(
        trade_id="T2", symbol="EURUSD", direction="BUY",
        entry=1.1, stop_loss=1.09, take_profit=1.11, lot_size=0.1,
        confidence=80.0, rationale="test",
    )
    store_pending_trade(trade)

    class FakeExecutor:
        def submit(self, request, approval):
            return type("R", (), {"ok": True, "ticket": 1, "price": 1.1})()

    result1 = asyncio.run(process_yes_approval("T2", FakeExecutor()))
    assert result1 == "executed"

    result2 = asyncio.run(process_yes_approval("T2", FakeExecutor()))
    assert result2 == "already_processed"


# ---------------------------------------------------------------------------
# 6. NO cancels and journals
# ---------------------------------------------------------------------------

def test_no_cancels_and_journals(monkeypatch):
    import engine.notifications.telegram_service as svc

    monkeypatch.setattr(svc, "_telegram_initialized", True)

    journal_events = []
    class FakeJournal:
        def log_event(self, *args, **kwargs):
            journal_events.append((args, kwargs))

    trade = PendingTrade(
        trade_id="T3", symbol="EURUSD", direction="BUY",
        entry=1.1, stop_loss=1.09, take_profit=1.11, lot_size=0.1,
        confidence=80.0, rationale="test",
    )
    store_pending_trade(trade, journal=FakeJournal())

    result = asyncio.run(process_no_approval("T3", journal=FakeJournal()))
    assert result == "cancelled"
    assert get_pending_trade("T3") is None
    assert len(journal_events) >= 1


# ---------------------------------------------------------------------------
# 7. Missing credentials make init_telegram return gracefully
# ---------------------------------------------------------------------------

def test_init_telegram_missing_credentials(monkeypatch):
    import engine.notifications.telegram_service as svc

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(svc, "_telegram_initialized", False)
    monkeypatch.setattr(svc, "_bot", None)

    assert asyncio.run(init_telegram()) is False


# ---------------------------------------------------------------------------
# 8. Bot token never appears in repr
# ---------------------------------------------------------------------------

def test_bot_token_not_in_repr():
    bot = _TelegramBot("SECRET123")
    repr_str = repr(bot)
    assert "SECRET123" not in repr_str


# ---------------------------------------------------------------------------
# 9. _TelegramBot.send_message uses requests (mocked)
# ---------------------------------------------------------------------------

def test_telegram_bot_send_message_mocked(monkeypatch):
    import engine.notifications.telegram_service as svc

    captured = {}
    def fake_post(url, json=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        return type("Resp", (), {"raise_for_status": lambda self: None, "json": lambda self: {}})()

    monkeypatch.setattr(svc.requests, "post", fake_post)
    bot = _TelegramBot("token123")
    bot.send_message(12345, "hello")
    assert captured["json"]["chat_id"] == 12345
    assert captured["json"]["text"] == "hello"


# ---------------------------------------------------------------------------
# 10. store_pending_trade rejects duplicates
# ---------------------------------------------------------------------------

def test_store_pending_trade_duplicate_protection():
    trade = PendingTrade(
        trade_id="T4", symbol="EURUSD", direction="BUY",
        entry=1.1, stop_loss=1.09, take_profit=1.11, lot_size=0.1,
        confidence=80.0, rationale="test",
    )
    assert store_pending_trade(trade) is True
    assert store_pending_trade(trade) is False
    assert pending_trade_count() == 1


# ---------------------------------------------------------------------------
# 11. cleanup_pending_trades clears cache
# ---------------------------------------------------------------------------

def test_cleanup_pending_trades():
    trade = PendingTrade(
        trade_id="T5", symbol="EURUSD", direction="BUY",
        entry=1.1, stop_loss=1.09, take_profit=1.11, lot_size=0.1,
        confidence=80.0, rationale="test",
    )
    store_pending_trade(trade)
    assert pending_trade_count() == 1
    cleanup_pending_trades()
    assert pending_trade_count() == 0
