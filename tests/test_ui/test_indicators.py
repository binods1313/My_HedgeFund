import math

from ui.indicators import (
    MAX_INDICATORS,
    atr,
    bollinger,
    bollinger_signals,
    clamp_indicators,
    ema,
    ema_cross_signals,
    format_fixed,
    macd,
    macd_cross_signals,
    plan_stops,
    rsi,
    rsi_signals,
)


def test_ema_matches_hand_seed() -> None:
    # span 3, seed (1+2+3)/3 = 2, k = 0.5, next = 4*0.5 + 2*0.5 = 3
    assert ema([1, 2, 3, 4], 3) == [None, None, 2, 3]


def test_rsi_matches_hand_average() -> None:
    # period 2. First value at index 2 is 50. Next bar lifts it to 75.
    values = rsi([1, 2, 1, 2], 2)
    assert values[2] == 50
    assert values[3] == 75


def test_atr_matches_hand_true_range() -> None:
    # Every true range is 2, so the seed and the next Wilder step stay 2.
    assert atr([2, 3, 4], [0, 1, 2], [1, 2, 3], 2) == [None, 2, 2]


def test_bollinger_uses_population_stdev() -> None:
    _mid, upper, lower = bollinger([1, 3, 5], 2, 2)
    assert upper[1] == 4
    assert lower[1] == 0
    assert upper[2] == 6
    assert lower[2] == 2


def test_macd_line_is_fast_minus_slow() -> None:
    line, _signal, _hist = macd([1, 2, 3, 4, 5], fast=2, slow=3, signal=2)
    fast = ema([1, 2, 3, 4, 5], 2)
    slow = ema([1, 2, 3, 4, 5], 3)
    assert line[2] == fast[2] - slow[2]
    assert line[1] is None


def test_future_bar_does_not_change_closed_values() -> None:
    close = [1, 2, 3, 4, 5, 6, 7, 8]
    high = [item + 0.5 for item in close]
    low = [item - 0.5 for item in close]
    before_line, before_signal, before_hist = macd(close, fast=3, slow=4, signal=2)
    before = {
        "ema": ema(close, 3),
        "rsi": rsi(close, 3),
        "macd": before_line,
        "macd_signal": before_signal,
        "macd_hist": before_hist,
        "atr": atr(high, low, close, 3),
        "bb": bollinger(close, 3, 2)[0],
    }
    mutated = close[:-1] + [1000]
    high[-1] = 1000
    low[-1] = 900
    after_line, after_signal, after_hist = macd(mutated, fast=3, slow=4, signal=2)
    after = {
        "ema": ema(mutated, 3),
        "rsi": rsi(mutated, 3),
        "macd": after_line,
        "macd_signal": after_signal,
        "macd_hist": after_hist,
        "atr": atr(high, low, mutated, 3),
        "bb": bollinger(mutated, 3, 2)[0],
    }
    for name in before:
        assert before[name][:-1] == after[name][:-1]
    closed = len(close) - 1
    assert macd_cross_signals(before_line, before_signal, upto=closed) == macd_cross_signals(
        after_line, after_signal, upto=closed
    )


def test_chart_macd_signal_is_an_ema_of_the_line() -> None:
    # A straight series can make MACD constant, so the signal matches it.
    # This series bends, so the signal and the histogram both move.
    values = [1, 3, 2, 5, 4, 8, 6, 9, 7, 10]
    line, signal, hist = macd(values, fast=3, slow=4, signal=2)
    paired = [
        (left, right, bar)
        for left, right, bar in zip(line, signal, hist)
        if bar is not None
    ]
    assert paired
    assert any(left != right for left, right, _bar in paired)
    assert any(bar != 0 for _left, _right, bar in paired)


def test_signal_rules_and_closed_bar_cutoff() -> None:
    fast = [1.0, 1.0, 5.0]
    slow = [2.0, 2.0, 2.0]
    assert ema_cross_signals(fast, slow, upto=2) == []
    crossed = ema_cross_signals(fast, slow, upto=3)
    assert crossed[0].side == "BUY" and crossed[0].index == 2

    assert rsi_signals([20.0, 40.0], upto=2)[0].side == "BUY"
    assert rsi_signals([80.0, 60.0], upto=2)[0].side == "SELL"
    assert rsi_signals([20.0, 40.0], upto=1) == []

    assert macd_cross_signals(fast, slow, upto=3)[0].side == "BUY"
    sell = bollinger_signals([11.0, 9.0], [10.0, 10.0], [0.0, 0.0], upto=2)
    buy = bollinger_signals([1.0, 3.0], [10.0, 10.0], [2.0, 2.0], upto=2)
    assert sell[0].side == "SELL"
    assert buy[0].side == "BUY"


def test_stop_plan_rr_and_jpy_tick() -> None:
    plan = plan_stops("BUY", 1.1, 0.001, atr_k=1.5, rr=3, tick_size=0.00001, point=0.00001)
    assert plan.ok
    assert math.isclose(plan.sl, 1.0985, abs_tol=1e-9)
    assert math.isclose(plan.tp, 1.1045, abs_tol=1e-9)

    yen = plan_stops("BUY", 150.0, 0.15, atr_k=1.5, rr=3, tick_size=0.001, point=0.001)
    assert yen.ok
    assert math.isclose(yen.sl, 149.775, abs_tol=1e-9)
    assert math.isclose(yen.tp, 150.675, abs_tol=1e-9)

    rejected = plan_stops(
        "BUY", 1.1, 0.001, atr_k=1.5, rr=3, tick_size=0.00001,
        stops_level_points=100, point=0.0001,
    )
    assert not rejected.ok
    assert "stops_level" in rejected.reason


def test_format_fixed_has_no_exponent() -> None:
    text = format_fixed(-3.7021e-05, 5)
    assert "e" not in text.lower()
    assert text == "-0.00004"


def test_clamp_stops_at_four() -> None:
    picked = clamp_indicators(["EMA20", "EMA50", "RSI", "MACD", "ATR", "Bollinger"])
    assert len(picked) == MAX_INDICATORS == 4
    assert clamp_indicators([]) == ["EMA20", "EMA50"]
