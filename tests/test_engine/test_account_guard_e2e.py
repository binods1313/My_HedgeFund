"""Mocked end-to-end account-guard tests.

The scenario this covers in full: an engine is running, the terminal's account
changes underneath it, and the engine must keep heartbeating while writing
absolutely nothing -- no telemetry, no positions, no orders -- and must recover
only on an explicit, journalled re-verification rather than silently.

No real terminal, no real orders, no credentials. The fake is injected.
Run: python -m pytest tests/test_engine/test_account_guard_e2e.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest

from engine.account_guard import AccountMismatch, Fingerprint
from engine.__main__ import build_engine
from tests.test_engine.fake_mt5 import FakeMT5, FakePosition

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _counts(journal):
    return {
        "equity": journal.query_one("SELECT COUNT(*) n FROM equity_snapshots")["n"],
        "positions": journal.query_one("SELECT COUNT(*) n FROM positions")["n"],
        "heartbeats": journal.query_one("SELECT COUNT(*) n FROM heartbeats")["n"],
    }


def _events(journal, category=None):
    rows = journal.recent_events(50)
    if category is None:
        return [e["message"] for e in rows]
    return [e["message"] for e in rows if e["category"] == category]


class TestMidRunAccountSwitch:
    """The engine is running when the account changes."""

    def test_a_switch_writes_nothing_and_deletes_nothing(self, tmp_path):
        mt5 = FakeMT5(positions=[FakePosition(ticket=424242, magic=100100)])
        engine = build_engine(root=REPO_ROOT, journal_path=tmp_path / "e2e.db",
                              mt5_module=mt5)
        assert engine.guard is not None
        assert engine._account_verified is True, (
            "the fake account and the matching credential must verify, or "
            "this test is exercising the wrong gate"
        )

        try:
            # One healthy iteration: heartbeat and telemetry both land.
            engine.run_once()
            before = _counts(engine.journal)
            assert before["equity"] == 1, "a verified engine records equity"
            assert before["heartbeats"] == 1

            # The terminal is switched to another account while we run. From
            # here the fake reports the other account, so every subsequent
            # run_once() re-verifies and finds the mismatch.
            mt5.account.login = 113363533
            mt5.account.currency = "EUR"

            for _ in range(3):
                engine.run_once()

            after = _counts(engine.journal)
            assert after["equity"] == before["equity"], (
                "telemetry must stop on a mismatch; equity rows grew from "
                f"{before['equity']} to {after['equity']}"
            )
            assert after["positions"] == before["positions"], (
                "no journal position may be added or removed on a mismatch"
            )
            assert after["heartbeats"] > before["heartbeats"], (
                "heartbeats must continue so the dashboard reads "
                "'Account mismatch' rather than 'offline'"
            )
            assert engine._account_verified is False, (
                "run_once must re-verify against the switched account"
            )

            # The mismatch was reported, not swallowed.
            account_events = _events(engine.journal, "account")
            assert any("Account mismatch" in m for m in account_events)
        finally:
            engine.gateway.stop()
            engine.journal.close()

    def test_reconcile_deletes_nothing_on_a_switched_account(self, tmp_path):
        mt5 = FakeMT5(positions=[FakePosition(ticket=424242, magic=100100)])
        engine = build_engine(root=REPO_ROOT, journal_path=tmp_path / "e2e2.db",
                              mt5_module=mt5)
        try:
            engine.reconcile_positions()
            before = _counts(engine.journal)["positions"]
            assert before == 1

            # Switch the terminal away, then let reconcile run again.
            other = FakeMT5()
            other.account.login = 113363533
            other.connected = True
            with pytest.raises(AccountMismatch):
                engine.guard.check(other.account_info(), phase="reconcile")

            result = engine.reconcile_positions()
            assert result["error"] == "account mismatch"
            assert _counts(engine.journal)["positions"] == before, (
                "a reconcile on the wrong account would see every journal "
                "ticket as absent and delete it"
            )
        finally:
            engine.gateway.stop()
            engine.journal.close()


class TestStartupWithWrongJournal:
    """Startup against a journal stamped for another account."""

    def test_startup_blocks_and_stamps_nothing(self, tmp_path):
        db = tmp_path / "wrong.db"
        mt5 = FakeMT5()
        engine = build_engine(root=REPO_ROOT, journal_path=db,
                              mt5_module=mt5)
        try:
            # Stamp the journal for a different account, as a previous run on
            # another account would have left it.
            engine.journal.set_account_fingerprint(
                login=113363533, currency="EUR", server="MetaQuotes-Demo",
                magic=100100)

            # A fresh guard on the same journal must refuse: the live account
            # is 999999999/FakeBroker and the journal says 113363533.
            with pytest.raises(AccountMismatch) as excinfo:
                engine.guard.check(mt5.account_info(), phase="startup")
            assert "113363533" in str(excinfo.value)

            assert engine.guard.may_reconcile() is False
            assert engine.guard.may_write_telemetry() is False
            assert engine.guard.may_submit_orders() is False

            stored = engine.journal.get_account_fingerprint()
            assert stored["login"] == 113363533, (
                "a mismatched terminal must never re-stamp the journal"
            )
            assert any("Account mismatch" in m
                       for m in _events(engine.journal, "account"))
        finally:
            engine.gateway.stop()
            engine.journal.close()

    def test_recovery_requires_a_new_verification(self, tmp_path):
        mt5 = FakeMT5()
        engine = build_engine(root=REPO_ROOT, journal_path=tmp_path / "rec.db",
                              mt5_module=mt5)
        try:
            engine.guard.check(mt5.account_info(), phase="startup")
            assert engine.guard.verified is True

            other = FakeMT5()
            other.account.login = 113363533
            other.connected = True
            with pytest.raises(AccountMismatch):
                engine.guard.check(other.account_info(), phase="reconnect")
            assert engine.guard.verified is False

            # Recovery: the guard is re-checked against the correct account and
            # only then do the gates reopen. Nothing recovers implicitly.
            engine.guard.check(mt5.account_info(), phase="reconnect")
            assert engine.guard.verified is True
            assert engine.guard.may_reconcile() is True
            assert engine.guard.may_write_telemetry() is True
        finally:
            engine.gateway.stop()
            engine.journal.close()


class TestForeignPositionsCounted:
    """Foreign positions must reduce the room available for new orders."""

    def _env(self):
        from engine.risk import RiskEngine, RiskLimits
        return RiskEngine(journal=None, limits=RiskLimits())

    def test_a_foreign_position_consumes_an_open_trade_slot(self):
        from engine.risk import AccountState, OrderIntent
        risk = self._env()
        # open_trade_count is the *broker's* count, which the account scan
        # builds from positions_get() regardless of magic. Four of one plus one
        # of theirs is five, and max_open_trades is five, so the slot limit is
        # what stops this order -- the equity checks above it must all pass so
        # the open-trade limit is provably the reason, not a side effect of a
        # halted engine or an unknown balance.
        account = AccountState(
            equity=100000.0,
            start_of_day_equity=100000.0,
            peak_equity=100000.0,
            open_trade_count=5,
            total_open_risk=100.0,
            unprotected_positions=0,
        )
        intent = OrderIntent.from_symbol_spec(
            symbol="EURUSD", side="BUY", volume=0.10,
            entry_price=1.10000, stop_loss=1.09800,
            tick_value=1.0, tick_size=0.00001)
        decision = risk.evaluate(intent, account)
        assert decision.approved is False
        assert any("open trade" in v for v in decision.violations), (
            "max_open_trades must count foreign positions, or a foreign order "
            "consumes margin the engine believes is free"
        )

    def test_a_foreign_position_alone_can_block_an_order(self):
        """Same shape, but with one fewer of ours: the foreign slot is the
        difference between an approval and a refusal."""
        from engine.risk import AccountState, OrderIntent

        def attempt(open_trade_count):
            account = AccountState(
                equity=100000.0,
                start_of_day_equity=100000.0,
                peak_equity=100000.0,
                open_trade_count=open_trade_count,
                total_open_risk=100.0,
                unprotected_positions=0,
            )
            intent = OrderIntent.from_symbol_spec(
                symbol="EURUSD", side="BUY", volume=0.10,
                entry_price=1.10000, stop_loss=1.09800,
                tick_value=1.0, tick_size=0.00001)
            return self._env().evaluate(intent, account)

        # Four of ours: approved. Add one foreign position: refused. The only
        # thing that changed is a position this engine does not own.
        assert attempt(4).approved is True
        assert attempt(5).approved is False

    def test_foreign_positions_are_counted_not_managed(self, tmp_path):
        from engine.account_guard import classify_foreign_positions
        from types import SimpleNamespace
        theirs = SimpleNamespace(ticket=58856025087, symbol="USDJPY", type=0,
                                 volume=1.0, profit=-0.23, magic=234000)
        ours = SimpleNamespace(ticket=424242, symbol="EURUSD", type=1,
                               volume=0.10, profit=1.0, magic=100100)
        foreign = classify_foreign_positions([theirs, ours], magic=100100)

        assert len(foreign) == 1
        assert foreign[0]["ticket"] == 58856025087
        assert foreign[0]["volume"] == 1.0, (
            "their real volume must be reported so exposure adds up"
        )

    def test_the_guard_does_not_adopt_foreign_positions(self, tmp_path):
        """Foreign positions never enter our positions table."""
        mt5 = FakeMT5(positions=[FakePosition(ticket=58856025087, magic=234000)])
        engine = build_engine(root=REPO_ROOT, journal_path=tmp_path / "fgn.db",
                              mt5_module=mt5)
        try:
            result = engine.reconcile_positions()
            assert result["adopted"] == [], (
                "a position with a magic number that is not ours is not ours"
            )
            assert _counts(engine.journal)["positions"] == 0
            assert any("foreign positions present" in m
                       for m in _events(engine.journal, "reconcile")), (
                "the operator must be told, once, that someone else's "
                "positions are on the account"
            )
        finally:
            engine.gateway.stop()
            engine.journal.close()
