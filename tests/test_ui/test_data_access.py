from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pandas as pd
import pytest

from engine.executor import LiveGuard, PaperExecutor
from engine.journal import Journal
from ui import data_access


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
    monkeypatch.setattr(data_access, "_read_engine_status", lambda: ("running", 1.0, "PAPER"))

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