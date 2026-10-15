"""Account fingerprint guard tests.

Every test here answers one question: can a journal written for account A
somehow be mutated while the terminal is on account B, or while the terminal
is unreadable?

Mocked throughout. No real terminal, no credentials, no orders.
Run: python -m pytest tests/test_engine/test_account_guard.py -v
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from engine.account_guard import (
    FOREIGN_MAGIC,
    AccountGuard,
    AccountMismatch,
    Fingerprint,
    TerminalOffline,
    classify_foreign_positions,
    fingerprint_from_account,
)
from engine.journal import Journal

CONFIGURED = Fingerprint(login=5057151780, currency="USD",
                         server="MetaQuotes-Demo")

LIVE_USD = SimpleNamespace(login=5057151780, server="MetaQuotes-Demo",
                           currency="USD")
LIVE_EUR = SimpleNamespace(login=5057151780, server="MetaQuotes-Demo",
                           currency="EUR")
LIVE_OTHER = SimpleNamespace(login=113363533, server="MetaQuotes-Demo",
                             currency="USD")


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "guard.db")
    yield j
    j.close()


def _guard(journal=None, configured=CONFIGURED, **kwargs) -> AccountGuard:
    return AccountGuard(journal=journal, configured=configured, **kwargs)


def _events(journal):
    return [e for e in journal.recent_events(50) if e["category"] == "account"]


def _positions(journal):
    return journal.query("SELECT ticket FROM positions")


# ─────────────────────────────────────────────────────────────────────────────
# Fingerprint comparison
# ─────────────────────────────────────────────────────────────────────────────

class TestFingerprint:

    def test_an_empty_fingerprint_matches_nothing(self):
        """A missing field is never a wildcard."""
        assert Fingerprint().matches(Fingerprint()) is False
        assert Fingerprint().matches(CONFIGURED) is False

    def test_a_missing_currency_is_a_mismatch(self):
        """Same login, unreported currency: still not a match.

        This is the case that would slip through if a missing currency were
        treated as "don't care": the login is right, so it looks verified.
        """
        silent = SimpleNamespace(login=5057151780, server="MetaQuotes-Demo",
                                 currency="")
        assert fingerprint_from_account(silent).matches(CONFIGURED) is False

    def test_a_different_currency_on_the_same_login_is_a_mismatch(self):
        assert fingerprint_from_account(LIVE_EUR).matches(CONFIGURED) is False

    def test_a_different_server_is_a_mismatch(self):
        moved = SimpleNamespace(login=5057151780, server="SomeOther-Demo",
                                currency="USD")
        assert fingerprint_from_account(moved).matches(CONFIGURED) is False

    def test_currency_is_normalised_only_by_case(self):
        assert Fingerprint(1, "usd").matches(Fingerprint(1, "USD")) is True


# ─────────────────────────────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────────────────────────────

class TestStartup:

    def test_a_matching_account_stamps_the_journal(self, journal):
        guard = _guard(journal)
        assert guard.check(LIVE_USD, phase="startup") is not None
        stored = journal.get_account_fingerprint()
        assert stored["login"] == 5057151780
        assert stored["currency"] == "USD", "currency comes from the broker"

    def test_a_mismatch_blocks_startup_and_journals_critical(self, journal):
        guard = _guard(journal)
        with pytest.raises(AccountMismatch):
            guard.check(LIVE_OTHER, phase="startup")

        events = _events(journal)
        assert events, "a startup mismatch must be journalled"
        assert events[-1]["severity"] == "CRITICAL"
        context = json.loads(events[-1]["context_json"])
        assert context["phase"] == "startup"
        assert "trading" in context["blocked"]
        assert "reconcile" in context["blocked"]
        assert "order_submission" in context["blocked"]

    def test_a_mismatch_never_stamps_the_new_account(self, journal):
        """The journal must keep belonging to the account it was made for."""
        journal.set_account_fingerprint(login=5057151780, currency="USD",
                                        server="MetaQuotes-Demo", magic=100100)
        guard = _guard(journal)
        with pytest.raises(AccountMismatch):
            guard.check(LIVE_OTHER, phase="startup")

        stored = journal.get_account_fingerprint()
        assert stored["login"] == 5057151780, (
            "a mismatched terminal must not re-stamp the journal"
        )

    def test_an_unreadable_account_blocks_startup(self, journal):
        """No account is not a pass."""
        guard = _guard(journal)
        with pytest.raises(AccountMismatch) as excinfo:
            guard.check(None, phase="startup")
        assert "no account" in str(excinfo.value)


# ─────────────────────────────────────────────────────────────────────────────
# Runtime account change — the incident this guard exists for
# ─────────────────────────────────────────────────────────────────────────────

class TestRuntimeAccountChange:

    def test_a_mid_session_switch_blocks_every_write(self, journal):
        guard = _guard(journal)
        guard.check(LIVE_USD, phase="startup")

        # The terminal is switched to another account while the engine runs.
        with pytest.raises(AccountMismatch):
            guard.check(LIVE_OTHER, phase="session_refresh")

        assert guard.may_reconcile() is False
        assert guard.may_write_telemetry() is False
        assert guard.may_submit_orders() is False
        assert guard.may_modify_orders() is False

    def test_reconcile_cannot_touch_the_journal_after_a_switch(self, journal):
        """The headline requirement: no journal position removed or modified.

        Reconcile deletes journal rows whose ticket is absent at the broker.
        After an account switch *every* row looks absent, so an unguarded
        reconcile would wipe the whole positions table in one pass.
        """
        guard = _guard(journal)
        guard.check(LIVE_USD, phase="startup")
        journal.upsert_position(ticket=424242, symbol="EURUSD", side="BUY",
                                volume=0.01, price_open=1.1, price_current=1.1,
                                sl=0.0, tp=0.0, profit=0.0, magic=100100)
        journal.upsert_position(ticket=434343, symbol="GBPUSD", side="SELL",
                                volume=0.02, price_open=1.2, price_current=1.2,
                                sl=0.0, tp=0.0, profit=0.0, magic=100100)
        before = _positions(journal)
        assert len(before) == 2

        with pytest.raises(AccountMismatch):
            guard.check(LIVE_OTHER, phase="reconnect")

        if guard.may_reconcile():
            journal.delete_position(424242)
            journal.delete_position(434343)

        assert _positions(journal) == before, (
            "a reconcile that ran on a mismatched account would have deleted "
            "both rows: on the other account those tickets do not exist"
        )

    def test_telemetry_is_blocked_after_a_switch(self, journal):
        guard = _guard(journal)
        guard.check(LIVE_USD, phase="startup")
        with pytest.raises(AccountMismatch):
            guard.check(LIVE_OTHER, phase="mt5_reinit")
        assert guard.may_write_telemetry() is False

    def test_a_recoverable_check_restores_the_gates(self, journal):
        """Switching back is not a permanent block; but it is re-verified."""
        guard = _guard(journal)
        guard.check(LIVE_USD)
        with pytest.raises(AccountMismatch):
            guard.check(LIVE_OTHER, phase="session_refresh")
        assert guard.may_reconcile() is False

        guard.check(LIVE_USD, phase="session_refresh")   # terminal restored
        assert guard.may_reconcile() is True
        assert guard.may_submit_orders() is True

    def test_each_phase_is_named_in_the_journal(self, journal, monkeypatch):
        """A call site is identifiable in the journal, phase by phase.

        Identical reasons inside one reminder window fold into one event, so
        the clock is advanced between phases: each event below is the
        reminder that reopens the incident, and it carries the phase that
        raised it.
        """
        import engine.account_guard as module
        current = [1000.0]
        monkeypatch.setattr(module.time, "monotonic", lambda: current[0])
        guard = _guard(journal)
        for phase in ("reconnect", "mt5_reinit", "session_refresh"):
            with pytest.raises(AccountMismatch):
                guard.check(LIVE_OTHER, phase=phase)
            current[0] += 601.0
        phases = {json.loads(e["context_json"])["phase"] for e in _events(journal)}
        assert phases == {"reconnect", "mt5_reinit", "session_refresh"}


# ─────────────────────────────────────────────────────────────────────────────
# Reconcile protection
# ─────────────────────────────────────────────────────────────────────────────

class TestReconcileProtection:

    def test_reconcile_is_blocked_while_mismatched(self, journal):
        guard = _guard(journal)
        guard.check(LIVE_USD)
        with pytest.raises(AccountMismatch):
            guard.check(LIVE_OTHER, phase="reconcile")
        assert guard.may_reconcile() is False

    def test_reconcile_is_allowed_only_on_a_matching_fingerprint(self, journal):
        guard = _guard(journal)
        guard.check(LIVE_USD)
        assert guard.may_reconcile() is True
        journal.set_account_fingerprint(login=999, currency="EUR", magic=1)
        guard2 = _guard(journal)
        with pytest.raises(AccountMismatch):
            guard2.check(LIVE_USD, phase="reconcile")
        assert guard2.may_reconcile() is False

    def test_a_mismatching_stored_fingerprint_blocks_reconcile(self, journal):
        """A journal stamped for another account cannot be reconciled into."""
        journal.set_account_fingerprint(login=113363533, currency="EUR",
                                        server="MetaQuotes-Demo", magic=100100)
        guard = _guard(journal)
        with pytest.raises(AccountMismatch) as excinfo:
            guard.check(LIVE_USD, phase="startup")
        assert "113363533" in str(excinfo.value), (
            "the message must name the account the journal belongs to"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Currency handling
# ─────────────────────────────────────────────────────────────────────────────

class TestCurrencyValidation:

    @pytest.mark.parametrize("live", [LIVE_USD, LIVE_EUR],
                             ids=["USD-account", "EUR-account"])
    def test_a_matching_currency_is_accepted(self, journal, live):
        """Currency comes from the broker, so neither account is special."""
        guard = _guard(journal, configured=Fingerprint(
            login=5057151780, currency=live.currency, server=live.Demo
            if hasattr(live, "Demo") else "MetaQuotes-Demo"))
        assert guard.check(live).currency == live.currency

    def test_currency_is_read_not_configured(self, journal):
        """The verified currency is the broker's, not the configured one."""
        guard = _guard(journal, configured=Fingerprint(
            login=5057151780, currency="USD", server="MetaQuotes-Demo"))
        assert guard.check(LIVE_USD).currency == "USD"

    def test_no_default_currency_is_assumed(self):
        """An unreadable account must not yield a currency."""
        assert fingerprint_from_account(None).currency is None
        assert fingerprint_from_account(
            SimpleNamespace(login=1, server="s", currency="")).currency is None


# ─────────────────────────────────────────────────────────────────────────────
# Foreign positions
# ─────────────────────────────────────────────────────────────────────────────

class TestForeignPositions:

    def test_foreign_positions_are_classified_not_adopted(self, journal):
        foreign = SimpleNamespace(ticket=58856025087, symbol="USDJPY",
                                  type=0, volume=0.01, profit=-0.23,
                                  magic=FOREIGN_MAGIC)
        mine = SimpleNamespace(ticket=700001, symbol="EURUSD", type=1,
                               volume=0.01, profit=1.0, magic=100100)

        found = classify_foreign_positions([foreign, mine], magic=100100)

        assert len(found) == 1
        assert found[0]["ticket"] == 58856025087
        assert found[0]["magic"] == FOREIGN_MAGIC
        assert all(row["ticket"] != 700001 for row in found), (
            "our own position must not be classified as foreign"
        )

    def test_an_unmapped_magic_is_not_ours(self):
        rows = classify_foreign_positions(
            [SimpleNamespace(ticket=1, symbol="EURUSD", type=0, volume=0.01,
                             profit=0.0, magic=234000)], magic=100100)
        assert rows and rows[0]["magic"] == 234000

    def test_the_retired_magic_is_recognised(self):
        assert FOREIGN_MAGIC == 234000

    def test_foreign_positions_are_counted_not_written(self, journal):
        """Counted for exposure; never written into our positions table."""
        foreign = [SimpleNamespace(ticket=58856025087, symbol="USDJPY",
                                   type=0, volume=0.01, profit=-0.23,
                                   magic=FOREIGN_MAGIC)]
        classified = classify_foreign_positions(foreign, magic=100100)
        assert journal.query("SELECT COUNT(*) n FROM positions")[0]["n"] == 0
        assert len(classified) == 1, (
            "the guard reports them; writing them is a different design"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Offline vs mismatch — the two incidents that block the same writes
# ─────────────────────────────────────────────────────────────────────────────

class TestOfflineVersusMismatch:

    def test_offline_is_a_mismatch_subclass(self):
        """Every existing ``except AccountMismatch`` must still catch it.

        The split exists for the journal and the operator, not for the catch
        sites: an unreadable terminal blocks exactly what a wrong account
        blocks, so the two cannot have unrelated exception hierarchies.
        """
        assert issubclass(TerminalOffline, AccountMismatch)

    def test_an_unreadable_terminal_raises_terminal_offline(self, journal):
        guard = _guard(journal)
        with pytest.raises(TerminalOffline) as excinfo:
            guard.check(None, phase="startup")
        assert "no account" in str(excinfo.value)

    def test_a_readable_wrong_account_raises_plain_mismatch(self, journal):
        guard = _guard(journal)
        with pytest.raises(AccountMismatch) as excinfo:
            guard.check(LIVE_OTHER, phase="startup")
        assert not isinstance(excinfo.value, TerminalOffline), (
            "a different account is a mismatch, not an outage"
        )

    def test_offline_is_journalled_as_a_warning_not_critical(self, journal):
        """An outage is not an incident of the same class as a wrong login.

        CRITICAL means "this system is about to act as the wrong account".
        A silent terminal cannot act at all -- it must still block, but the
        severity tells the operator where to look first.
        """
        guard = _guard(journal)
        with pytest.raises(TerminalOffline):
            guard.check(None, phase="session_refresh")
        events = _events(journal)
        assert events[-1]["severity"] == "WARNING"
        assert events[-1]["message"].startswith("Terminal offline")
        context = json.loads(events[-1]["context_json"])
        assert context["kind"] == "terminal_offline"

    def test_a_mismatch_is_still_critical(self, journal):
        guard = _guard(journal)
        with pytest.raises(AccountMismatch):
            guard.check(LIVE_OTHER, phase="session_refresh")
        events = _events(journal)
        assert events[-1]["severity"] == "CRITICAL"
        context = json.loads(events[-1]["context_json"])
        assert context["kind"] == "account_mismatch"

    def test_both_block_every_write_gate(self, journal):
        """The split changes wording and severity only, never the blocking."""
        guard = _guard(journal)
        with pytest.raises(TerminalOffline):
            guard.check(None, phase="startup")
        assert guard.may_submit_orders() is False
        assert guard.may_reconcile() is False
        assert guard.may_write_telemetry() is False
        assert guard.may_modify_orders() is False


# ─────────────────────────────────────────────────────────────────────────────
# Rate limiting — one event per change, one reminder per window
# ─────────────────────────────────────────────────────────────────────────────

class TestRejectionRateLimit:

    @pytest.fixture
    def clock(self, monkeypatch):
        """A controllable monotonic clock, in seconds from an arbitrary epoch."""
        import engine.account_guard as module
        current = [1000.0]
        monkeypatch.setattr(module.time, "monotonic", lambda: current[0])
        return current

    def test_a_persistent_outage_journals_once_then_reminds(self, journal, clock):
        guard = _guard(journal)
        for _ in range(500):
            with pytest.raises(TerminalOffline):
                guard.check(None, phase="session_refresh")
        assert len(_events(journal)) == 1, (
            "500 identical rejections in one window are one incident"
        )

        clock[0] += 601.0
        with pytest.raises(TerminalOffline):
            guard.check(None, phase="session_refresh")
        assert len(_events(journal)) == 2
        # recent_events is newest-first: [0] is the reminder just written.
        newest = json.loads(_events(journal)[0]["context_json"])
        assert newest["repeat"] is True

    def test_a_changed_reason_logs_immediately(self, journal, clock):
        guard = _guard(journal)
        with pytest.raises(TerminalOffline):
            guard.check(None, phase="session_refresh")
        # The terminal comes back but on another account: a new incident,
        # not a repeat of the outage, despite the same window.
        with pytest.raises(AccountMismatch):
            guard.check(LIVE_OTHER, phase="session_refresh")
        assert len(_events(journal)) == 2
        newest = json.loads(_events(journal)[0]["context_json"])
        assert newest["repeat"] is False

    def test_recovery_makes_the_same_reason_a_new_incident(self, journal, clock):
        guard = _guard(journal)
        with pytest.raises(TerminalOffline):
            guard.check(None, phase="session_refresh")
        assert guard.check(LIVE_USD, phase="session_refresh") is not None
        recovered = [
            event for event in _events(journal)
            if json.loads(event["context_json"]).get("kind") == "terminal_recovered"
        ]
        assert len(recovered) == 1
        assert recovered[0]["severity"] == "INFO"

        # Back within the same window: the outage ended and began again, which
        # is exactly the change an operator needs to see. The recovery row is
        # the one between the two offline rows, and it does not swallow the
        # second outage.
        clock[0] += 30.0
        with pytest.raises(TerminalOffline):
            guard.check(None, phase="session_refresh")
        kinds = [
            json.loads(event["context_json"]).get("kind")
            for event in _events(journal)
        ]
        assert kinds.count("terminal_offline") == 2, (
            "a recurrence after recovery must not be swallowed as a repeat"
        )
        assert kinds.count("terminal_recovered") == 1
        newest = json.loads(_events(journal)[0]["context_json"])
        assert newest["repeat"] is False


# ─────────────────────────────────────────────────────────────────────────────
# The NULL-stamp race
# ─────────────────────────────────────────────────────────────────────────────

class TestNullStampRace:

    def test_a_null_login_is_never_stamped(self, journal):
        """The racing writer that knows nothing must write nothing.

        The old ``INSERT OR REPLACE`` with default-NULL arguments blanked a
        real stamp, and the blank then read as "unstamped" -- so the next
        account to verify would adopt a journal that belonged to someone
        else. The race is closed at the write itself.
        """
        journal.set_account_fingerprint()          # all-NULL call
        journal.set_account_fingerprint(currency="USD", magic=100100)
        assert journal.get_account_fingerprint() is None

    def test_a_null_stamp_cannot_erase_a_real_one(self, journal):
        journal.set_account_fingerprint(login=5057151780, currency="USD",
                                        server="MetaQuotes-Demo", magic=100100)
        journal.set_account_fingerprint()          # the racer, arriving late
        stored = journal.get_account_fingerprint()
        assert stored["login"] == 5057151780
        assert stored["currency"] == "USD"

    def test_a_preexisting_null_row_does_not_block_a_real_stamp(self, journal):
        """A journal already carrying NULLs (written before this fix) recovers."""
        journal.execute("INSERT INTO journal_account "
                        "(id, login, currency, server, magic, recorded_at) "
                        "VALUES (1, NULL, NULL, NULL, NULL, '2026-01-01T00:00:00Z')")
        guard = _guard(journal)
        assert guard.check(LIVE_USD, phase="startup") is not None
        stored = journal.get_account_fingerprint()
        assert stored["login"] == 5057151780


# ─────────────────────────────────────────────────────────────────────────────
# A test that cannot be vacuous
# ─────────────────────────────────────────────────────────────────────────────

def test_the_guard_actually_raises(tmp_path) -> None:
    """Positive control: this whole file must be able to fail."""
    guard = AccountGuard(journal=None, configured=CONFIGURED)
    with pytest.raises(AccountMismatch):
        guard.check(LIVE_OTHER, phase="startup")
    assert guard.mismatch_reason is not None
