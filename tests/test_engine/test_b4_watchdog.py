"""B4: a blocked owner thread is journalled and orders are not retried.

The blocker is a fake account_info, not an order. The test releases the
blocker before it finishes so the owner thread does not outlive the test.
"""
from __future__ import annotations

import json
import threading
import time

from engine.config import GatewaySettings, load_engine_config
from engine.gateway import Gateway
from engine.journal import Journal
from tests.test_engine.fake_mt5 import FakeMT5
from tests.test_engine.test_gateway import TEST_LOGIN, TEST_SECRET, TEST_SERVER, make_gateway


def test_defaults_are_thirty_seconds_and_three_attempts():
    settings = GatewaySettings()
    assert settings.watchdog_timeout_seconds == 30.0
    assert settings.account_reinitialize_attempts == 3
    loaded = load_engine_config()
    assert loaded.engine.gateway.watchdog_timeout_seconds == 30.0
    assert loaded.engine.gateway.account_reinitialize_attempts == 3


def test_a_blocked_owner_call_is_a_stall_and_orders_are_not_retried(tmp_path):
    journal = Journal(tmp_path / "stall.db")
    mt5 = FakeMT5()
    gateway = make_gateway(
        mt5, journal, watchdog_timeout=0.05, call_timeout=2.0,
        login=TEST_LOGIN, password=TEST_SECRET, server=TEST_SERVER,
    )
    entered = threading.Event()
    release = threading.Event()
    saved = mt5.account_info

    def blocking():
        entered.set()
        release.wait(timeout=5)
        return saved()

    mt5.account_info = blocking
    caller = threading.Thread(target=gateway.account_info, name="stall-caller")
    try:
        caller.start()
        assert entered.wait(timeout=2), "the owner thread never entered the call"
        time.sleep(0.12)
        gateway.poll_watchdog()
        gateway.poll_watchdog()

        assert gateway.connected is False
        assert gateway.account_readable is False
        stalled = []
        for event in journal.recent_events(20):
            context = json.loads(event["context_json"] or "{}")
            if context.get("kind") == "gateway_stalled":
                stalled.append((event, context))
        assert len(stalled) == 1, "a stall is journalled once"
        event, context = stalled[0]
        assert event["severity"] == "WARNING"
        assert context["pending_call"] == "account_info"
        assert context["age_seconds"] >= 0.05
        assert "order_send" not in mt5.call_names()

        refused = gateway.send_order(
            {"symbol": "EURUSD", "volume": 0.01, "type": "BUY"})
        assert refused.ok is False
        assert "order_send" not in mt5.call_names()

        journal.heartbeat(
            "IDLE", connected=False, gateway_call_age=gateway.last_success_age())
        beat = journal.query_one(
            "SELECT connected, gateway_call_age_seconds FROM heartbeats "
            "ORDER BY id DESC LIMIT 1")
        assert beat["connected"] == 0
        assert beat["gateway_call_age_seconds"] is not None
    finally:
        release.set()
        caller.join(timeout=3)
        mt5.account_info = saved
        gateway.stop()
        journal.close()


def test_the_next_read_reconnects_without_sending_an_order(tmp_path):
    journal = Journal(tmp_path / "reconnect.db")
    mt5 = FakeMT5()
    gateway = make_gateway(mt5, journal, watchdog_timeout=0.05, call_timeout=2.0)
    entered = threading.Event()
    release = threading.Event()
    saved = mt5.account_info

    def blocking():
        entered.set()
        release.wait(timeout=5)
        return saved()

    mt5.account_info = blocking
    caller = threading.Thread(target=gateway.account_info)
    try:
        caller.start()
        assert entered.wait(timeout=2)
        time.sleep(0.12)
        gateway.poll_watchdog()
        release.set()
        caller.join(timeout=3)
        mt5.account_info = saved
        gateway.status.next_retry_at = 0
        before_orders = mt5.call_names().count("order_send")
        account = gateway.account_info()
        assert account is not None
        assert gateway.connected is True
        assert mt5.call_names().count("shutdown") >= 1
        assert mt5.call_names().count("initialize") >= 2
        assert mt5.call_names().count("order_send") == before_orders
    finally:
        release.set()
        if caller.is_alive():
            caller.join(timeout=3)
        gateway.stop()
        journal.close()


def test_constructing_a_gateway_uses_the_thirty_second_default(tmp_path):
    journal = Journal(tmp_path / "default.db")
    gateway = Gateway(journal=journal, mt5_module=FakeMT5())
    try:
        assert gateway.watchdog_timeout == 30.0
        assert gateway.max_account_reinitializes == 3
    finally:
        journal.close()
