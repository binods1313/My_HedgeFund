"""OTE Option 1, approved stop/TP, and ScanCycle PENDING with SL/TP."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from engine.indicators.structure import (
    Sweep,
    last_completed_swing,
    mean_true_range,
)
from engine.journal import Journal
from engine.scan_cycle import ScanCycle
from engine.strategy import (
    BrokerStops,
    Signal,
    attach_trade_levels,
    broker_stops_from_view,
    evaluate,
    sweep_stop,
)
from tests.test_engine.test_indicators import flat, frame
from tests.test_engine.test_signal_e2e import _new_engine, _make_history

EUR = BrokerStops(point=0.00001, tick_size=0.00001, stops_level_points=0, spread_points=10)


def _impulse_then_pullback(n_up=40, n_down=40, n_start=20):
    rows = []
    price = 1.1200
    for _ in range(n_start):
        o = price
        price -= 0.0004
        rows.append((o, o + 0.00005, price - 0.00005, price))
    for _ in range(n_up):
        o = price
        price += 0.0005
        rows.append((o, price + 0.00005, o - 0.00005, price))
    for _ in range(n_down):
        o = price
        price -= 0.0002
        rows.append((o, o + 0.00005, price - 0.00005, price))
    return frame(rows)


def test_no_completed_swing_on_a_short_flat_frame() -> None:
    assert last_completed_swing(flat(10)) is None


def test_completed_swing_ends_at_the_pivot_not_the_last_close() -> None:
    df = _impulse_then_pullback()
    leg = last_completed_swing(df)
    assert leg is not None
    last_close = float(df["close"].iloc[-1])
    assert leg.end_price != pytest.approx(last_close)
    assert leg.end_index < len(df) - 1


def test_tiny_span_is_not_an_ote_leg() -> None:
    df = flat(320, price=1.10)
    signal = evaluate("EURUSD", df, direction="BUY")
    ote = next(b for b in signal.blocks if b.name == "fibonacci OTE")
    assert ote.agrees is False
    assert "no completed swing" in ote.detail or signal.leg is None


def test_multiple_legs_uses_the_last_completed_pair() -> None:
    rows = []
    price = 1.10
    for cycle in range(3):
        for _ in range(20):
            o = price
            price += 0.0004
            rows.append((o, price + 0.00005, o - 0.00005, price))
        for _ in range(12):
            o = price
            price -= 0.00015
            rows.append((o, o + 0.00005, price - 0.00005, price))
    df = frame(rows)
    leg = last_completed_swing(df)
    assert leg is not None
    earlier = last_completed_swing(df.iloc[:-20])
    if earlier is not None:
        assert leg.end_index >= earlier.end_index


def test_one_tick_stop_is_rejected_or_pushed_to_atr() -> None:
    sweep = Sweep(index=10, timestamp=None, direction="BUY",
                  level=1.16478, extreme=0.00001, reclaimed=True)
    got = sweep_stop("BUY", 1.16477, sweep, atr=0.00100, spec=EUR)
    assert got is not None
    stop, dist, min_dist = got
    assert dist == pytest.approx(0.00100)
    assert min_dist == pytest.approx(0.00100)
    assert 1.16477 - stop >= 0.00100 - 1e-12


def test_attach_sets_two_r_take_profit() -> None:
    sweep = Sweep(index=10, timestamp=None, direction="BUY",
                  level=1.10000, extreme=0.00050, reclaimed=True)
    df = _impulse_then_pullback()
    signal = Signal(symbol="EURUSD", direction="BUY", price=1.10100, sweep=sweep)
    attach_trade_levels(signal, df, EUR)
    assert signal.stop_loss is not None
    assert signal.take_profit is not None
    dist = signal.price - signal.stop_loss
    assert signal.take_profit == pytest.approx(signal.price + 2.0 * dist)
    assert dist + 1e-12 >= mean_true_range(df)


def test_scancycle_pending_has_sl_tp_expiry(tmp_path) -> None:
    def strategy_fn(symbol, df):
        sweep = Sweep(index=len(df) - 1, timestamp=None, direction="BUY",
                      level=1.0890, extreme=0.00040, reclaimed=True)
        return Signal(
            symbol=symbol, direction="BUY", price=1.0900,
            sweep=sweep, blocks=[], confidence=1.0,
        )

    journal, engine = _new_engine(
        tmp_path, approved=True, strategy_fn=strategy_fn, df=_make_history(),
        volume_caps={"EURUSD": 10.0},
    )
    scanner = ScanCycle(symbols=["EURUSD"], interval_seconds=0, broker_offset_seconds=0)
    outcome = scanner.run_with_engine(engine)
    assert outcome.signals == 1
    row = journal.query(
        "SELECT status, stop_loss, take_profit, expires_at, entry_price, "
        "reject_reason, decision_id FROM signals"
    )[0]
    assert row["status"] == "PENDING"
    assert row["stop_loss"] is not None
    assert row["take_profit"] is not None
    assert row["expires_at"] is not None
    assert row["reject_reason"] is None
    assert abs(float(row["take_profit"]) - float(row["entry_price"])) == pytest.approx(
        2.0 * abs(float(row["entry_price"]) - float(row["stop_loss"])), rel=1e-6
    )
    journal.close()


@pytest.mark.parametrize("k", [0, 1, 2, 3, 4])
def test_live_like_frame_stop_is_at_least_one_atr(k) -> None:
    df = _impulse_then_pullback(n_up=30 + k, n_down=25 + k)
    sweep = Sweep(
        index=len(df) - 1, timestamp=None, direction="SELL",
        level=float(df["high"].iloc[-1]) - 0.0001,
        extreme=0.0003, reclaimed=True,
    )
    entry = float(df["close"].iloc[-1])
    signal = Signal(symbol="EURUSD", direction="SELL", price=entry, sweep=sweep)
    attach_trade_levels(signal, df, EUR)
    if signal.stop_loss is None:
        pytest.skip("frame did not clear stop quality")
    atr = mean_true_range(df)
    assert signal.stop_loss - entry + 1e-12 >= atr
    assert signal.take_profit == pytest.approx(entry - 2.0 * (signal.stop_loss - entry))
