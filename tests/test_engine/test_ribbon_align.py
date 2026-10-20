"""BUY maps to BULLISH and SELL to BEARISH on the EMA ribbon block."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from engine.indicators.trend import ema_ribbon
from engine.strategy import evaluate, ribbon_matches_side
from tests.test_engine.test_indicators import frame
from tests.test_engine.test_strategy_dryrun import trending_rows

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ribbon_buy_bullish.csv"


def _ribbon_block(signal):
    return next(b for b in signal.blocks if b.name == "ema ribbon")


def test_buy_maps_to_bullish_and_sell_to_bearish() -> None:
    assert ribbon_matches_side("BUY", "BULLISH") is True
    assert ribbon_matches_side("BUY", "BEARISH") is False
    assert ribbon_matches_side("BUY", "MIXED") is False
    assert ribbon_matches_side("SELL", "BEARISH") is True
    assert ribbon_matches_side("SELL", "BULLISH") is False
    assert ribbon_matches_side("SELL", "MIXED") is False
    assert ribbon_matches_side(None, "BULLISH") is False
    assert ribbon_matches_side("BUY", "BULLISH") is not (
        "BUY" == "BULLISH"
    )


def test_buy_plus_bullish_stack_agrees() -> None:
    df = trending_rows(direction=1.0, n=320, seed=5)
    assert ema_ribbon(df).direction == "BULLISH"
    block = _ribbon_block(evaluate("EURUSD", df, direction="BUY"))
    assert block.agrees is True
    assert "BULLISH" in block.detail


def test_buy_plus_bearish_or_mixed_disagrees() -> None:
    down = trending_rows(direction=-1.0, n=320, seed=5)
    assert ema_ribbon(down).direction == "BEARISH"
    assert _ribbon_block(evaluate("EURUSD", down, direction="BUY")).agrees is False
    from tests.test_engine.test_indicators import flat
    mixed = flat(320)
    assert ema_ribbon(mixed).direction == "MIXED"
    assert _ribbon_block(evaluate("EURUSD", mixed, direction="BUY")).agrees is False


def test_sell_plus_bearish_agrees_bullish_or_mixed_disagrees() -> None:
    down = trending_rows(direction=-1.0, n=320, seed=5)
    assert ema_ribbon(down).direction == "BEARISH"
    assert _ribbon_block(evaluate("EURUSD", down, direction="SELL")).agrees is True
    up = trending_rows(direction=1.0, n=320, seed=5)
    assert ema_ribbon(up).direction == "BULLISH"
    assert _ribbon_block(evaluate("EURUSD", up, direction="SELL")).agrees is False
    from tests.test_engine.test_indicators import flat
    assert _ribbon_block(evaluate("EURUSD", flat(320), direction="SELL")).agrees is False


def test_regression_stored_bullish_frame_agrees_on_buy() -> None:
    """A frozen CSV: the ribbon must stay BULLISH and agree with BUY."""
    df = pd.read_csv(FIXTURE, parse_dates=["time"])
    assert ema_ribbon(df).direction == "BULLISH"
    block = _ribbon_block(evaluate("EURUSD", df, direction="BUY"))
    assert block.agrees is True
    assert ribbon_matches_side("BUY", "BULLISH") is True
    assert ribbon_matches_side("BUY", ema_ribbon(df).direction) is True
