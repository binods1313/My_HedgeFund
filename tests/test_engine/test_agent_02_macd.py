"""Agent 02 MACD: the signal is an EMA of the MACD line, with no look-ahead."""

from __future__ import annotations

import math
from fractions import Fraction

import numpy as np

from engine.research.agent_02_technical import AGENT_VERSION, macd_series, run


def _hand_ema(values: list[Fraction], period: int) -> list[Fraction]:
    alpha = Fraction(2, period + 1)
    out = [values[0]]
    for price in values[1:]:
        out.append(alpha * price + (1 - alpha) * out[-1])
    return out


def test_hand_computed_histogram_is_not_zero() -> None:
    # prices 1,2,3,4; fast 2 (alpha 2/3), slow 3 (alpha 1/2), signal 2.
    # EMA2 ends at 95/27. EMA3 ends at 25/8. MACD = 85/216.
    # Signal EMA of that MACD series ends at 37/108. Histogram = 11/216.
    prices = [Fraction(item) for item in (1, 2, 3, 4)]
    line = [fast - slow for fast, slow in zip(_hand_ema(prices, 2), _hand_ema(prices, 3))]
    signal = _hand_ema(line, 2)
    assert line[-1] == Fraction(85, 216)
    assert signal[-1] == Fraction(37, 108)
    assert line[-1] - signal[-1] == Fraction(11, 216)

    got_line, got_signal, got_hist = macd_series(
        np.array([1.0, 2.0, 3.0, 4.0]), fast=2, slow=3, signal=2
    )
    assert math.isclose(got_line[-1], float(Fraction(85, 216)))
    assert math.isclose(got_signal[-1], float(Fraction(37, 108)))
    assert math.isclose(got_hist[-1], float(Fraction(11, 216)))
    assert got_hist[-1] != 0.0
    assert got_signal[-1] != got_line[-1]


def test_macd_series_does_not_look_ahead() -> None:
    prices = np.array([1.0, 2.0, 3.0, 4.0, 3.0, 2.0, 4.0, 5.0])
    full = macd_series(prices, fast=2, slow=3, signal=2)
    for end in range(1, len(prices) + 1):
        prefix = macd_series(prices[:end], fast=2, slow=3, signal=2)
        for series, earlier in zip(full, prefix):
            assert math.isclose(float(series[end - 1]), float(earlier[-1]))


def test_run_histogram_uses_the_signal_series() -> None:
    prices = np.linspace(1.10, 1.25, 40)
    result = run({"prices": prices})
    assert result.ok is True
    assert result.features["histogram"] != 0.0
    assert AGENT_VERSION == "2"
