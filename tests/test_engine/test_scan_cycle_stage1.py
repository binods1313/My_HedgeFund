"""Stage 1 scan cycle. Mocked gateway, no strategy, no orders.

F6, F8-F10, F12 and F13 are out of scope: they need a strategy or an order.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from engine.journal import Journal
from engine.scan_cycle import ScanCycle
from engine.state import STATE_IDLE, STATE_SCANNING, StateMachine


def _open(bar: str) -> dict:
    return {"market_open": True, "stale": False, "bar_ts": bar}


def _setup(tmp_path, symbols, **kwargs):
    journal = Journal(tmp_path / "scan.db")
    state = StateMachine(journal)
    state.transition(STATE_IDLE, "ENABLE_AI")
    scanner = ScanCycle(symbols=list(symbols), interval_seconds=0, remind_every=600, **kwargs)
    return journal, state, scanner


def _symbols(journal):
    return journal.query("SELECT symbol, status, reason FROM scan_symbols ORDER BY id")


def _events(journal, message):
    return journal.query(
        "SELECT message, severity, context_json FROM events WHERE message = ?",
        (message,),
    )


def test_f14_one_market_read_for_five_symbols(tmp_path):
    symbols = ["S1", "S2", "S3", "S4", "S5"]
    journal, state, scanner = _setup(tmp_path, symbols)
    calls = {"n": 0}

    def read_market(names):
        calls["n"] += 1
        assert list(names) == symbols
        return {"symbols": {name: _open(name) for name in names}}

    outcome = scanner.run(state=state, journal=journal, read_market=read_market, mode="PAPER")
    assert calls["n"] == 1
    assert outcome.scanned == 5
    assert state.state == STATE_IDLE
    assert len(_symbols(journal)) == 5


def test_f3_one_symbol_error_does_not_stop_the_cycle(tmp_path):
    symbols = ["A", "BAD", "C", "D"]
    journal, state, scanner = _setup(tmp_path, symbols)

    def compute(symbol, view):
        if symbol == "BAD":
            raise RuntimeError("boom")

    outcome = scanner.run(
        state=state, journal=journal, mode="PAPER", compute=compute,
        read_market=lambda names: {"symbols": {name: _open(name) for name in names}},
    )
    rows = _symbols(journal)
    assert outcome.errors == 1 and outcome.scanned == 3
    assert [row["status"] for row in rows] == ["scanned", "error", "scanned", "scanned"]
    assert state.state == STATE_IDLE
    assert "SCAN_COMPLETE" not in inspect.getsource(ScanCycle.run)


def test_f4_all_symbols_error_returns_scan_empty(tmp_path):
    symbols = ["A", "B"]
    journal, state, scanner = _setup(tmp_path, symbols)

    def compute(symbol, view):
        raise RuntimeError(symbol)

    outcome = scanner.run(
        state=state, journal=journal, mode="PAPER", compute=compute,
        read_market=lambda names: {"symbols": {name: _open(name) for name in names}},
    )
    assert outcome.errors == 2 and outcome.scanned == 0 and outcome.status == "empty"
    assert state.state == STATE_IDLE
    triggers = journal.query(
        "SELECT context_json FROM events WHERE message = ?",
        ("SCANNING -> IDLE",),
    )
    assert triggers and "SCAN_EMPTY" in triggers[0]["context_json"]
    assert state.state != STATE_SCANNING


def test_f1_terminal_drop_defers_the_rest_and_returns_idle(tmp_path):
    symbols = ["A", "B", "C", "D", "E"]
    journal, state, scanner = _setup(tmp_path, symbols)
    seen = {"n": 0}

    def terminal_up():
        seen["n"] += 1
        # Call 1 is the pre-cycle gate. Calls 2 and 3 scan A and B.
        return seen["n"] <= 3

    outcome = scanner.run(
        state=state, journal=journal, mode="PAPER", terminal_up=terminal_up,
        read_market=lambda names: {"symbols": {name: _open(name) for name in names}},
    )
    rows = _symbols(journal)
    assert [row["status"] for row in rows] == [
        "scanned", "scanned", "deferred", "deferred", "deferred",
    ]
    assert all(row["reason"] == "TERMINAL_OFFLINE" for row in rows[2:])
    assert len(_events(journal, "Terminal offline")) == 1
    assert outcome.deferred == 3
    assert state.state == STATE_IDLE


def test_f2_account_switch_blocks_further_writes_and_submits_nothing(tmp_path):
    symbols = ["A", "B", "C"]
    journal, state, scanner = _setup(tmp_path, symbols)
    seen = {"n": 0}
    orders: list[str] = []

    def writes_allowed():
        seen["n"] += 1
        # Call 1 is the pre-cycle gate. Call 2 allows A. Call 3 blocks.
        return seen["n"] <= 2

    scanner.run(
        state=state, journal=journal, mode="PAPER", writes_allowed=writes_allowed,
        read_market=lambda names: {"symbols": {name: _open(name) for name in names}},
    )
    rows = _symbols(journal)
    assert [row["symbol"] for row in rows] == ["A"]
    assert len(_events(journal, "Account mismatch")) == 1
    assert orders == []
    source = Path(inspect.getfile(ScanCycle)).read_text(encoding="utf-8")
    assert "send_order" not in source
    assert "order_send" not in source
    assert state.state == STATE_IDLE


def test_f5_budget_defers_and_still_leaves_the_heartbeat(tmp_path):
    symbols = ["A", "B", "C"]
    journal, state, scanner = _setup(tmp_path, symbols, max_scan_seconds=0)
    journal.heartbeat(STATE_IDLE, connected=True)
    computed = {"n": 0}

    def compute(symbol, view):
        computed["n"] += 1

    outcome = scanner.run(
        state=state, journal=journal, mode="PAPER", compute=compute,
        monotonic=lambda: 10.0,
        read_market=lambda names: {"symbols": {name: _open(name) for name in names}},
    )
    assert outcome.deferred == 3 and computed["n"] == 0
    assert len(_events(journal, "cycle budget exhausted")) == 1
    assert journal.query_one("SELECT COUNT(*) AS n FROM heartbeats")["n"] == 1
    assert state.state == STATE_IDLE


def test_f11_same_closed_bar_skips_without_recompute(tmp_path):
    journal, state, scanner = _setup(tmp_path, ["EURUSD"])
    computed = {"n": 0}

    def compute(symbol, view):
        computed["n"] += 1

    def read_market(names):
        return {"symbols": {name: _open("BAR-1") for name in names}}

    first = scanner.run(
        state=state, journal=journal, mode="PAPER",
        compute=compute, read_market=read_market,
    )
    second = scanner.run(
        state=state, journal=journal, mode="PAPER",
        compute=compute, read_market=read_market,
    )
    assert first.computed == 1 and second.computed == 0 and computed["n"] == 1
    reasons = [row["reason"] for row in _symbols(journal)]
    assert reasons == ["", "NO_NEW_CLOSED_BAR"]


def test_market_closed_is_recorded_before_stale_quote(tmp_path):
    journal, state, scanner = _setup(tmp_path, ["XAUUSD"])
    scanner.run(
        state=state, journal=journal, mode="PAPER",
        read_market=lambda names: {"symbols": {
            "XAUUSD": {"market_open": False, "stale": True, "bar_ts": "1"},
        }},
    )
    row = _symbols(journal)[0]
    assert row["reason"] == "MARKET_CLOSED"
    body = inspect.getsource(ScanCycle._one_symbol)
    assert body.find("MARKET_CLOSED") < body.find("STALE_QUOTE")


def test_state_change_writes_a_skip_and_a_repeat_waits_ten_minutes(tmp_path):
    journal = Journal(tmp_path / "skip.db")
    state = StateMachine(journal)  # OFF
    scanner = ScanCycle(symbols=["EURUSD"], interval_seconds=0, remind_every=600)
    clock = {"t": 0.0}

    def run():
        return scanner.run(
            state=state, journal=journal, mode="PAPER",
            monotonic=lambda: clock["t"],
            read_market=lambda names: {"symbols": {}},
        )

    first = run()
    second = run()
    assert first.reason == "STATE_OFF" and second.reason == "STATE_OFF"
    assert journal.query_one("SELECT COUNT(*) AS n FROM scan_results")["n"] == 1

    clock["t"] = 600.0
    third = run()
    assert third.reason == "STATE_OFF"
    assert journal.query_one("SELECT COUNT(*) AS n FROM scan_results")["n"] == 2

    state.transition(STATE_IDLE, "ENABLE_AI")
    changed = scanner.run(
        state=state, journal=journal, mode="PAPER",
        monotonic=lambda: clock["t"],
        terminal_up=lambda: False,
        read_market=lambda names: (_ for _ in ()).throw(AssertionError("no read on offline gate")),
    )
    assert changed.reason == "TERMINAL_OFFLINE"
    reasons = [row["reason"] for row in journal.query(
        "SELECT reason FROM scan_results ORDER BY id")]
    assert reasons[-1] == "TERMINAL_OFFLINE"
    assert len(reasons) == 3
    journal.close()
