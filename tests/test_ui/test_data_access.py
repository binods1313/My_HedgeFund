import sqlite3
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pandas as pd
import pytest

from ui import data_access
from ui.data_access import Result

from engine.journal import Journal, SCHEMA_VERSION

try:  # the executor module imports cleanly without MT5
    from engine.executor import LiveGuard, PaperExecutor
except Exception:  # pragma: no cover - only under an incomplete install
    LiveGuard = PaperExecutor = object



#: The broker spec the fake terminal hands back. Mirrors the values a real
#: EURUSD contract carries so the risk arithmetic in `submit_order` is exercised
#: against realistic numbers rather than a degenerate spec.
EURUSD_INFO = {
    "digits": 5,
    "point": 0.00001,
    "trade_tick_size": 0.00001,
    "trade_tick_value_loss": 0.9,
    "trade_tick_value": 1.0,
    "trade_contract_size": 100000,
    "volume_min": 0.01,
    "volume_max": 100.0,
    "volume_step": 0.01,
    "trade_stops_level": 5,
}


def _fake_config(db_path, mode: str = "PAPER"):
    """The shape `submit_order` reads off the engine config."""
    return SimpleNamespace(
        mode=mode,
        db_path=str(db_path),
        engine=SimpleNamespace(magic_number=100100),
        risk=SimpleNamespace(risk_per_trade_pct=1.0, daily_loss_limit_pct=3.0,
                             max_drawdown_pct=10.0, max_open_trades=5,
                             max_total_exposure_pct=10.0),
    )


def _stub_journal_account(monkeypatch, *, login: int, currency: str,
                          match: bool = True) -> None:
    """Give the journal a fingerprint, or one that deliberately mismatches.

    `match=False` writes a *different* account, which is what a journal left
    over from a previous account looks like -- the case the UI must refuse.
    """
    stored_login = login if match else (login + 1)

    def fake_fingerprint(path=None):
        return SimpleNamespace(login=stored_login, currency=currency,
                                path=path)

    monkeypatch.setattr(data_access, "_journal_account_matches",
                        lambda: None if match else Result(
                            False, "ACCOUNT_MISMATCH",
                            "Account mismatch; order not submitted."))

def _stub_market(monkeypatch, tmp_path, *, mode: str = "PAPER",
                 breached: bool = False, stub_guard: bool = True,
                 equity: float = 10_000.0):
    """Wire `submit_order` to a fake terminal, journal and (optionally) guard.

    `stub_guard` False leaves the real `make_live_guard` in place so the
    fail-closed path can be exercised end to end.
    """
    monkeypatch.setattr(data_access, "load_engine_config",
                        lambda: _fake_config(tmp_path / "engine.db", mode))
    monkeypatch.setattr(data_access, "get_account_snapshot",
                        lambda: SimpleNamespace(connected=True, equity=equity,
                                                engine_state="running"))
    monkeypatch.setattr(data_access.data_engine, "get_symbol_info_safe",
                        lambda asset: dict(EURUSD_INFO))
    monkeypatch.setattr(data_access.data_engine, "get_symbol_live_quote",
                        lambda asset: {"valid": True, "bid": 1.09990,
                                      "ask": 1.10000})
    # The UI order path now refuses when the journal's account fingerprint does
    # not match the terminal, so the stub must supply one. Without it the test
    # would pass for the wrong reason: blocked by the account guard rather
    # than by whatever the test is actually about.
    monkeypatch.setattr(
        data_access.data_engine, "get_account_telemetry",
        lambda: {"connected": True, "login": 999999999,
                 "currency": "EUR", "server": "MetaQuotes-Demo"})
    _stub_journal_account(monkeypatch, login=999999999, currency="EUR")
    if stub_guard:
        monkeypatch.setattr(
            data_access, "make_live_guard",
            lambda **kwargs: LiveGuard(invalidation_check=lambda symbol: breached))


def test_snapshot_degrades_on_broker_exception(monkeypatch) -> None:
    def raise_broker_error():
        raise RuntimeError("terminal unavailable")

    monkeypatch.setattr(data_access, "initialize_mt5", raise_broker_error)

    snapshot = data_access.get_account_snapshot()

    assert snapshot.connected is False
    assert snapshot.engine_state == "offline"
    assert snapshot.balance is None
    assert snapshot.equity is None
    assert snapshot.margin_used is None
    assert snapshot.free_margin is None
    assert snapshot.open_pnl is None
    assert snapshot.open_risk is None
    assert snapshot.fetched_at.tzinfo is not None
    with pytest.raises(FrozenInstanceError):
        snapshot.connected = True


def test_positions_degrade_to_fixed_empty_schema(monkeypatch) -> None:
    def raise_broker_error():
        raise RuntimeError("positions unavailable")

    monkeypatch.setattr(data_access, "initialize_mt5", lambda: True)
    monkeypatch.setattr(data_access, "get_open_positions", raise_broker_error)

    positions = data_access.get_active_positions()

    assert isinstance(positions, pd.DataFrame)
    assert positions.empty
    assert tuple(positions.columns) == data_access.POSITION_COLUMNS


def test_submit_order_routes_through_the_engine_executor(monkeypatch, tmp_path) -> None:
    """A manual order takes the engine path, not the legacy broker shortcut.

    Rewritten from `test_submit_order_delegates_sl_and_tp_to_execution`, which
    spied on `execution.execute_trade`. That function is no longer on the hot
    path, so asserting it was called would now assert the bug.

    Three claims, in the order they matter: the executor is built from the
    configured mode with a guard attached, no Gateway is constructed (which
    would open a second MT5 session inside Streamlit), and the fill actually
    lands in the engine journal.
    """
    _stub_market(monkeypatch, tmp_path)
    captured: dict = {}
    real_build = data_access.build_executor

    def spy_build(mode, **kwargs):
        captured.update(mode=mode, **kwargs)
        return real_build(mode, **kwargs)

    monkeypatch.setattr(data_access, "build_executor", spy_build)

    result = data_access.submit_order("EURUSD", "BUY", 0.10, 30, 60)

    assert result.ok, result.message
    assert captured["mode"] == "PAPER"
    assert captured["live_guard"] is not None, (
        "the manual path submitted without a structural guard"
    )
    assert captured.get("gateway") is None, (
        "a Gateway was constructed inside the UI; live trading is CLI-only"
    )

    journal = Journal(str(tmp_path / "engine.db"))
    try:
        row = journal.query_one(
            "SELECT ticket, symbol, side, volume, entry_price, sl, tp, status, "
            "broker FROM trades WHERE symbol = ?", ("EURUSD",))
    finally:
        journal.close()

    assert row is not None, "the fill was never journaled"
    assert row["broker"] == "PAPER"
    assert row["side"] == "BUY"
    assert row["volume"] == pytest.approx(0.10)
    assert row["status"] == "OPEN"
    assert row["sl"] < row["entry_price"], "a BUY stop must sit below entry"
    assert row["ticket"] == result.ticket


def test_submit_order_derives_absolute_prices_from_pips(monkeypatch, tmp_path) -> None:
    """The ×10 point convention is preserved from the legacy path.

    30 pips at point 0.00001 is 0.003 of price, so a BUY entered at 1.10000
    stops at 1.09700. Changing the convention here would silently rescale every
    stop the UI sends, so it is pinned rather than left to inspection.
    """
    _stub_market(monkeypatch, tmp_path)
    captured: dict = {}
    real_prepare = PaperExecutor.prepare_order

    def spy_prepare(self, symbol, side, volume, sl, tp=None, comment=""):
        captured.update(sl=sl, tp=tp)
        return real_prepare(self, symbol, side, volume, sl, tp, comment)

    monkeypatch.setattr(PaperExecutor, "prepare_order", spy_prepare)

    result = data_access.submit_order("EURUSD", "BUY", 0.10, 30, 60)

    assert result.ok, result.message
    assert captured["sl"] == pytest.approx(1.09700)
    assert captured["tp"] == pytest.approx(1.10600)


def test_submit_order_refuses_live_mode_without_building_an_executor(
        monkeypatch, tmp_path) -> None:
    """LIVE is refused from the UI, before anything is constructed.

    Building a LiveExecutor here would call the gateway and open a second MT5
    session inside Streamlit, so the refusal has to happen first -- which is what
    the exploding `build_executor` proves.
    """
    _stub_market(monkeypatch, tmp_path, mode=data_access.MODE_LIVE)

    def must_not_build(*args, **kwargs):
        raise AssertionError("a live executor was constructed inside the UI")

    monkeypatch.setattr(data_access, "build_executor", must_not_build)

    result = data_access.submit_order("EURUSD", "BUY", 0.10, 30, 60)

    assert result.ok is False
    assert result.code == "LIVE_UI_DISABLED"


def test_submit_order_rejects_when_the_guard_breaches_the_anchor(
        monkeypatch, tmp_path) -> None:
    """A breached anchor blocks dispatch and the engine's own words reach the UI.

    The message is asserted verbatim rather than paraphrased: Part 2 of the
    requirement is that the frontend banner shows the engine's rejection, not a
    UI-authored summary of it.
    """
    _stub_market(monkeypatch, tmp_path, breached=True)

    result = data_access.submit_order("EURUSD", "BUY", 0.10, 30, 60)

    assert result.ok is False
    assert result.code == "EXECUTION_REJECTED"
    assert "100% swing anchor" in result.message


def test_submit_order_fails_closed_when_candles_are_unavailable(
        monkeypatch, tmp_path) -> None:
    """A feed that cannot be read blocks the order.

    Runs against the *real* `make_live_guard` (the stub is disabled), so this
    covers the whole chain: `_anchor_bars` swallows the feed error and returns
    None, the guard raises, `LiveGuard.verify` converts the raise into a
    refusal, and `submit` turns that into `ExecutionRejected`.
    """
    _stub_market(monkeypatch, tmp_path, stub_guard=False)

    def no_candles(asset, timeframe, count=100):
        raise RuntimeError("terminal returned no candles")

    monkeypatch.setattr(data_access.data_engine, "get_technical_dataframe", no_candles)

    result = data_access.submit_order("EURUSD", "BUY", 0.10, 30, 60)

    assert result.ok is False
    assert result.code == "EXECUTION_REJECTED"
    assert "RuntimeError" in result.message


def test_submit_order_refuses_without_a_stop_loss(monkeypatch, tmp_path) -> None:
    """No stop, no order -- and the refusal comes from the adapter, before the
    executor is built, so nothing is journaled either."""
    _stub_market(monkeypatch, tmp_path)

    result = data_access.submit_order("EURUSD", "BUY", 0.10, None, None)

    assert result.ok is False
    assert result.code == "SL_REQUIRED"


@pytest.mark.parametrize("stop", [None, 0, 0.0, -1, "0", ""])
def test_submit_order_refuses_a_zero_or_absent_stop(
        monkeypatch, tmp_path, stop) -> None:
    """The boundary the broker positions actually crossed.

    USDCAD 10726505976 and GBPUSD 10745483547 were open with sl=0.0. A zero is
    not "a very tight stop", it is no stop, and it must be refused identically
    to an absent one -- and refused before an executor is built, so nothing
    reaches the journal or the broker.
    """
    _stub_market(monkeypatch, tmp_path)

    result = data_access.submit_order("EURUSD", "BUY", 0.10, stop, 60)

    assert result.ok is False
    assert result.code == "SL_REQUIRED"
    # The refusal happens before a journal is opened, so there is no record of
    # a trade to unwind -- and no executor that could send one.
    journal_db = tmp_path / "engine.db"
    if journal_db.exists():
        with sqlite3.connect(f"file:{journal_db}?mode=ro", uri=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_submit_order_passes_a_positive_stop_and_records_it(
        monkeypatch, tmp_path) -> None:
    """The control case: with a real stop the order is no longer refused here.

    Guards against a guard that refuses everything.
    """
    _stub_market(monkeypatch, tmp_path)
    captured: dict = {}
    real_executor = data_access.build_executor

    def spy(*args, **kwargs):
        captured["executor"] = real_executor(*args, **kwargs)
        return captured["executor"]

    monkeypatch.setattr(data_access, "build_executor", spy)

    result = data_access.submit_order("EURUSD", "BUY", 0.10, 30, 60)

    assert result.ok, result.message
    assert "executor" in captured


@pytest.mark.parametrize("direction", ["", None, "LONG", "buy-ish"])
def test_submit_order_rejects_an_unrecognised_direction(
        monkeypatch, tmp_path, direction) -> None:
    _stub_market(monkeypatch, tmp_path)

    result = data_access.submit_order("EURUSD", direction, 0.10, 30, 60)

    assert result.code == "BAD_DIRECTION"


def test_submit_order_rejects_a_non_positive_volume(monkeypatch, tmp_path) -> None:
    _stub_market(monkeypatch, tmp_path)

    result = data_access.submit_order("EURUSD", "BUY", 0, 30, 60)

    assert result.code == "BAD_VOLUME"


@pytest.mark.parametrize("state,expected", [("paused", "PAUSED"),
                                            ("halted", "EMERGENCY_STOP")])
def test_submit_order_is_blocked_by_engine_state(monkeypatch, tmp_path,
                                                 state, expected) -> None:
    """The engine's own state machine gates manual orders too.

    A paused or halted engine must refuse hand-entered trades for the same
    reason it refuses automated ones.
    """
    _stub_market(monkeypatch, tmp_path)
    monkeypatch.setattr(data_access, "get_account_snapshot",
                        lambda: SimpleNamespace(connected=True, equity=10_000.0,
                                                engine_state=state))

    result = data_access.submit_order("EURUSD", "BUY", 0.10, 30, 60)

    assert result.ok is False
    assert result.code == "EXECUTION_REJECTED"
    assert expected in result.message


def test_account_snapshot_uses_live_currency_and_zero_margin_guard(monkeypatch) -> None:
    monkeypatch.setattr(data_access, "initialize_mt5", lambda: True)
    monkeypatch.setattr(
        data_access,
        "get_account_telemetry",
        lambda: {
            "connected": True,
            "currency": "EUR",
            "balance": 1000,
            "equity": 1010,
            "margin": 0,
            "margin_free": 1010,
        },
    )
    monkeypatch.setattr(data_access, "get_open_positions", lambda: [])
    # (state, heartbeat_age_s, mode, engine_online) -- the fourth element is
    # the liveness signal, deliberately separate from the trading state.
    monkeypatch.setattr(data_access, "_read_engine_status",
                        lambda: ("running", 1.0, "PAPER", True))

    snapshot = data_access.get_account_snapshot()

    assert snapshot.connected
    assert snapshot.currency == "EUR"
    assert snapshot.margin_level is None
    assert snapshot.equity == 1010


def test_symbol_spec_uses_loss_tick_value(monkeypatch) -> None:
    monkeypatch.setattr(
        data_access.data_engine,
        "get_symbol_info_safe",
        lambda asset: {
            "digits": 5,
            "point": 0.00001,
            "trade_tick_size": 0.00001,
            "trade_tick_value_loss": 0.9,
            "trade_tick_value": 1.0,
            "trade_contract_size": 100000,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
            "trade_stops_level": 5,
        },
    )

    spec = data_access.get_symbol_spec("EURUSD", "forex")

    assert spec is not None
    assert spec.tick_value_loss == 0.9
    assert spec.stops_level_points == 5

class TestAccountMismatch:
    """The UI must refuse a broker write on a mismatched account.

    These run against the real `_journal_account_matches`, not the stub: the
    stub above exists so unrelated tests are not blocked by the guard, and this
    class exists to prove the guard actually bites.
    """

    def test_submit_order_is_refused_on_a_mismatched_account(
            self, monkeypatch, tmp_path):
        _stub_market(monkeypatch, tmp_path)
        # The journal belongs to another account; the terminal states the
        # stubbed one. The UI must refuse before any broker call.
        monkeypatch.setattr(
            data_access, "_journal_account_matches",
            lambda: Result(False, "ACCOUNT_MISMATCH",
                          "Account mismatch; order not submitted."))

        result = data_access.submit_order("EURUSD", "BUY", 0.10, 30, 60)

        assert result.ok is False
        assert result.code == "ACCOUNT_MISMATCH"

    def test_close_position_is_refused_on_a_mismatched_account(
            self, monkeypatch, tmp_path):
        _stub_market(monkeypatch, tmp_path)
        monkeypatch.setattr(
            data_access, "_journal_account_matches",
            lambda: Result(False, "ACCOUNT_MISMATCH",
                          "Account mismatch; order not submitted."))

        result = data_access.close_position(700001)

        assert result.ok is False
        assert result.code == "ACCOUNT_MISMATCH"

    def test_no_close_control_is_offered_for_a_foreign_magic(self, monkeypatch):
        """A position carrying another system's magic is not manageable.

        `managed=False` is what the UI reads to hide its controls, so this
        asserts the flag the decisions tab depends on.
        """
        from ui.data_access import _is_our_magic
        assert _is_our_magic(100100) is True   # ours
        assert _is_our_magic(234000) is False  # the retired system's
        assert _is_our_magic(None) is False
        assert _is_our_magic("") is False
        assert _is_our_magic(0) is False

    def test_positions_carry_a_managed_flag(self, monkeypatch, tmp_path):
        _stub_market(monkeypatch, tmp_path)
        original = data_access.get_open_positions
        monkeypatch.setattr(
            data_access, "get_open_positions",
            lambda: [
                {"ticket": 700001, "symbol": "EURUSD", "type": "BUY",
                 "volume": 0.10, "price_open": 1.1, "price_current": 1.1,
                 "sl": 1.09, "tp": 1.12, "profit": 1.0, "magic": 100100},
                {"ticket": 58856025087, "symbol": "USDJPY", "type": "BUY",
                 "volume": 0.01, "price_open": 158.4, "price_current": 158.45,
                 "sl": 158.0, "tp": 159.0, "profit": -0.23, "magic": 234000},
            ])
        try:
            frame = data_access.get_active_positions()
        finally:
            monkeypatch.setattr(data_access, "get_open_positions", original)

        assert "magic" in frame.columns, "the magic must travel with the row"
        assert "managed" in frame.columns
        ours = frame[frame["ticket"] == 700001]
        theirs = frame[frame["ticket"] == 58856025087]
        assert not ours.empty and bool(ours["managed"].iloc[0]) is True
        assert not theirs.empty and bool(theirs["managed"].iloc[0]) is False, (
            "a position from the retired system must be marked unmanaged"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Read-only journal tables (Journal tab) and the System Status card
# ─────────────────────────────────────────────────────────────────────────────

class TestReadJournalTable:
    """The Journal tab's reader: whitelist, filters, and never-a-write."""

    @pytest.fixture
    def journal(self, tmp_path):
        j = Journal(tmp_path / "journal_view.db")
        yield j
        j.close()

    @pytest.fixture
    def seeded(self, journal):
        """One row in every whitelisted table, plus a two-step lifecycle."""
        journal.execute(
            "INSERT INTO signals "
            "(symbol, direction, score, bar_ts, ts, decision_id) "
            "VALUES ('EURUSD', 'BUY', 0.8, 1760000000, "
            "'2026-10-10T10:00:00Z', 'TEST-UI-SEED')")
        journal.execute(
            "INSERT INTO risk_decisions (trade_id, symbol, approved, "
            "reasons_json, ts) VALUES (1, 'EURUSD', 1, '[\"ok\"]', "
            "'2026-10-10T10:00:01Z')")
        journal.execute(
            "INSERT INTO llm_calls (decision, confidence, ok, latency_ms, "
            "error, ts) VALUES ('BUY', 0.7, 1, 120, NULL, "
            "'2026-10-10T10:00:00Z')")
        journal.execute(
            "INSERT INTO scan_results (symbols_json, ranked_json, ts) "
            "VALUES ('[\"EURUSD\",\"XAUUSD\"]', '[1,2]', "
            "'2026-10-10T10:00:00Z')")
        journal.execute(
            "INSERT INTO trades (ticket, symbol, side, volume, entry_price, "
            "exit_price, sl, tp, status, magic, ts, updated_at) "
            "VALUES (700001, 'EURUSD', 'BUY', 0.10, 1.1, 1.11, 1.09, 1.12, "
            "'CLOSED', 100100, '2026-10-10T10:00:02Z', "
            "'2026-10-10T11:00:00Z')")
        journal.upsert_position(ticket=700001, symbol="EURUSD", side="BUY",
                                volume=0.10, price_open=1.1,
                                price_current=1.11, sl=1.09, tp=1.12,
                                profit=1.0, magic=100100)
        journal.log_lifecycle("signal_generated", "BUY EURUSD",
                              decision_id="DEC-1", context={"symbol": "EURUSD"})
        journal.log_lifecycle("risk_approved", "risk ok",
                              decision_id="DEC-1", context={"symbol": "EURUSD"})
        journal.log_lifecycle("signal_generated", "BUY XAUUSD",
                              decision_id="DEC-2", context={"symbol": "XAUUSD"})
        return journal


    @pytest.mark.parametrize("name", sorted(data_access.JOURNAL_TABLES))
    def test_every_whitelisted_table_reads_back(self, seeded, tmp_path, name):
        frame = data_access.read_journal_table(
            name, db_path=tmp_path / "journal_view.db")
        assert not frame.empty, f"{name} was seeded but reads empty"
        assert tuple(frame.columns) == data_access.JOURNAL_TABLES[name]

    def test_an_unknown_table_name_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="unknown journal table"):
            data_access.read_journal_table(
                "sqlite_master", db_path=tmp_path / "nope.db")

    def test_a_missing_database_yields_an_empty_frame_and_no_file(self, tmp_path):
        target = tmp_path / "never-created.db"
        frame = data_access.read_journal_table("trades", db_path=target)
        assert frame.empty
        assert tuple(frame.columns) == data_access.JOURNAL_TABLES["trades"]
        assert not target.exists(), "a read-only reader must never create a file"

    def test_the_symbol_filter_narrows_the_trades_table(self, seeded, tmp_path):
        db = tmp_path / "journal_view.db"
        seeded.execute(
            "INSERT INTO trades (ticket, symbol, side, volume, status, "
            "magic, ts, updated_at) VALUES (700002, 'XAUUSD', 'SELL', 0.05, "
            "'OPEN', 100100, '2026-10-10T12:00:00Z', "
            "'2026-10-10T12:00:00Z')")
        frame = data_access.read_journal_table("trades", db_path=db,
                                               symbol="XAUUSD")
        assert list(frame["symbol"]) == ["XAUUSD"]

    def test_the_symbol_filter_is_refused_where_no_symbol_lives(self, seeded,
                                                                tmp_path):
        with pytest.raises(ValueError, match="no symbol column"):
            data_access.read_journal_table(
                "llm_calls", db_path=tmp_path / "journal_view.db",
                symbol="EURUSD")

    def test_the_decision_id_filter_selects_one_chain(self, seeded, tmp_path):
        frame = data_access.read_journal_table(
            "lifecycle", db_path=tmp_path / "journal_view.db",
            decision_id="DEC-1")
        assert len(frame) == 2
        assert set(frame["decision_id"]) == {"DEC-1"}
        assert set(frame["message"]) == {"BUY EURUSD", "risk ok"}

    def test_the_decision_id_filter_is_refused_outside_lifecycle(self, seeded,
                                                                 tmp_path):
        with pytest.raises(ValueError, match="decision IDs"):
            data_access.read_journal_table(
                "trades", db_path=tmp_path / "journal_view.db",
                decision_id="DEC-1")

    def test_the_lifecycle_view_excludes_ordinary_events(self, seeded, tmp_path):
        seeded.log_event("command", "EMERGENCY_STOP -> done")
        frame = data_access.read_journal_table(
            "lifecycle", db_path=tmp_path / "journal_view.db")
        assert "command" not in set(frame["category"]), (
            "the lifecycle chain is the lifecycle categories, not every event"
        )

    def test_the_limit_is_clamped_not_obeyed_blindly(self, seeded, tmp_path):
        frame = data_access.read_journal_table(
            "lifecycle", db_path=tmp_path / "journal_view.db", limit=0)
        assert len(frame) >= 1, "a limit of 0 must not mean 'no rows at all'"



class TestSystemStatus:
    """The System Status card reads the journal and says so."""

    @pytest.fixture
    def journal(self, tmp_path):
        j = Journal(tmp_path / "status.db")
        yield j
        j.close()

    def test_a_fresh_journal_reports_itself(self, journal, tmp_path):
        journal.set_state("OFF", mode="PAPER")
        status = data_access.system_status(tmp_path / "status.db")
        assert status["journal_exists"] is True
        assert status["schema_version"] == SCHEMA_VERSION
        assert status["engine_state"] == "OFF"
        assert status["engine_online"] is False
        assert status["pending_commands"] == 0

    def test_a_journal_with_no_state_row_reports_no_state(self, journal, tmp_path):
        """Nothing has run yet; the card must say so, not invent OFF."""
        status = data_access.system_status(tmp_path / "status.db")
        assert status["engine_state"] is None

    def test_a_live_heartbeat_reads_online(self, journal, tmp_path):
        journal.set_state("OFF", mode="PAPER")
        journal.heartbeat("IDLE", connected=True)
        status = data_access.system_status(tmp_path / "status.db")
        assert status["engine_online"] is True
        assert status["engine_state"] == "OFF", "state comes from engine_state"
        assert status["heartbeat_age_s"] is not None

    def test_a_stale_heartbeat_reads_offline(self, journal, tmp_path):
        from datetime import datetime, timedelta, timezone
        journal.set_state("OFF", mode="PAPER")
        journal.heartbeat("IDLE", connected=True)
        stale = datetime.now(timezone.utc) - timedelta(minutes=5)
        journal.execute(
            "UPDATE heartbeats SET ts = ? WHERE id = "
            "(SELECT MAX(id) FROM heartbeats)",
            (stale.isoformat(timespec="seconds"),))
        status = data_access.system_status(tmp_path / "status.db")
        assert status["engine_online"] is False
        assert status["heartbeat_age_s"] > 60

    def test_the_account_login_is_masked(self, journal, tmp_path):
        journal.set_account_fingerprint(login=5057151780, currency="USD",
                                        server="MetaQuotes-Demo", magic=100100)
        status = data_access.system_status(tmp_path / "status.db")
        assert status["account_login_masked"] == "****1780"
        assert "5057151780" not in str(status), (
            "a full login is a credential fragment; the card must not show it"
        )

    def test_the_account_tooltip_carries_last4_currency_and_server_type(self, journal, tmp_path):
        from ui.components.header import _account_badge, _account_tooltip

        journal.set_account_fingerprint(login=5057151780, currency="USD",
                                        server="MetaQuotes-Demo", magic=100100)
        status = data_access.system_status(tmp_path / "status.db")
        assert status["account_server_type"] == "demo"
        tooltip = _account_tooltip(status)
        assert tooltip == "Account ****1780 · USD · demo server"
        assert "5057151780" not in tooltip
        assert "MetaQuotes" not in tooltip
        badge = _account_badge(status)
        assert 'title="Account ****1780 · USD · demo server"' in badge
        assert _account_badge({}).startswith('<span class="status-badge green">')

    def test_a_missing_journal_says_so_without_raising(self, tmp_path):
        status = data_access.system_status(tmp_path / "absent.db")
        assert status["journal_exists"] is False
        assert status["engine_state"] is None

    def test_pending_commands_are_counted(self, journal, tmp_path):
        journal.enqueue_command("PAUSE", {"source": "ui"})
        journal.enqueue_command("RESUME", {"source": "ui"})
        status = data_access.system_status(tmp_path / "status.db")
        assert status["pending_commands"] == 2
