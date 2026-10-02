"""Behaviour tests for metrics.py.

These replace source-text assertions. The previous tests read app.py and
checked for substrings, which was fragile (they broke on formatting changes)
and, more importantly, the first attempt at them still passed while a mutation
was live because they recomputed the expected value locally instead of
exercising the code that actually runs.

Every test here calls a real function with real data and asserts on the
observable output.

Run: python -m pytest tests/test_metrics.py -v
"""
import math

import pytest

import metrics
from metrics import (
    NO_SCORE,
    compute_open_risk,
    format_llm_confidence,
    format_net_open_pnl,
    format_risk_colour,
    gross_exposure,
    net_open_pnl,
    net_open_pnl_colour,
)


def _pos(ticket, symbol, volume, entry, sl, tp=None, profit=0.0):
    return {"ticket": ticket, "symbol": symbol, "volume": volume,
            "price_open": entry, "sl": sl, "tp": tp, "profit": profit}


def _info(tick_value=1.0, tick_size=0.00001):
    return {"trade_tick_value": tick_value, "trade_tick_size": tick_size}


# ─────────────────────────────────────────────────────────────────────────────
# Net open P&L  (the sign bug)
# ─────────────────────────────────────────────────────────────────────────────

class TestNetOpenPnl:

    def test_losses_sum_to_a_negative_total(self):
        positions = [_pos(1, "USDCAD", 0.01, 1.418, 1.42, profit=-3.89),
                     _pos(2, "GBPUSD", 0.01, 1.329, 1.33, profit=-6.30)]
        assert net_open_pnl(positions) == pytest.approx(-10.19)

    def test_a_loss_is_never_rendered_as_a_gain(self):
        """The regression: this used to display '+EUR 10.19' in green."""
        positions = [_pos(1, "USDCAD", 0.01, 1.418, 1.42, profit=-3.89),
                     _pos(2, "GBPUSD", 0.01, 1.329, 1.33, profit=-6.30)]
        rendered = format_net_open_pnl(positions, "EUR")
        assert rendered == "-€10.19", f"loss rendered as {rendered!r}"
        assert not rendered.startswith("+")

    def test_profits_render_with_plus(self):
        positions = [_pos(1, "EURUSD", 0.1, 1.085, 1.080, profit=12.5)]
        assert format_net_open_pnl(positions, "EUR") == "+€12.50"

    def test_mixed_positions_net_out_signed(self):
        positions = [_pos(1, "A", 0.1, 1.0, 0.9, profit=12.0),
                     _pos(2, "B", 0.1, 1.0, 0.9, profit=-3.0)]
        assert net_open_pnl(positions) == pytest.approx(9.0)
        assert format_net_open_pnl(positions, "EUR") == "+€9.00"

    def test_loss_colour_is_red(self):
        positions = [_pos(1, "A", 0.1, 1.0, 0.9, profit=-5.0)]
        assert net_open_pnl_colour(positions) == "#F43F5E"

    def test_profit_colour_is_green(self):
        positions = [_pos(1, "A", 0.1, 1.0, 0.9, profit=5.0)]
        assert net_open_pnl_colour(positions) == "#34D399"

    def test_no_positions_is_zero(self):
        assert net_open_pnl([]) == 0.0
        assert format_net_open_pnl([], "EUR") == "+€0.00"

    def test_non_numeric_profit_is_treated_as_zero(self):
        positions = [_pos(1, "A", 0.1, 1.0, 0.9, profit=None)]
        assert net_open_pnl(positions) == 0.0

    def test_gross_exposure_discards_sign(self):
        positions = [_pos(1, "A", 0.1, 1.0, 0.9, profit=-3.89),
                     _pos(2, "B", 0.1, 1.0, 0.9, profit=6.30)]
        assert gross_exposure(positions) == pytest.approx(10.19)
        assert net_open_pnl(positions) == pytest.approx(2.41)


# ─────────────────────────────────────────────────────────────────────────────
# Open risk: entry-to-stop exposure, not P&L
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeOpenRisk:

    def test_risk_is_distance_from_entry_to_stop(self):
        """100 ticks (0.00100 at tick_size 1e-5) at 1.0 tick_value, 1 lot => 100."""
        positions = [_pos(1, "EURUSD", 1.0, 1.10000, 1.09900)]
        summary = compute_open_risk(positions, {"EURUSD": _info()}, equity=1_000_000)
        assert summary.total_risk_amount == pytest.approx(100.0)

    def test_one_tick_of_stop_distance_is_one_currency_unit(self):
        positions = [_pos(1, "EURUSD", 1.0, 1.10000, 1.09999)]
        summary = compute_open_risk(positions, {"EURUSD": _info()}, equity=1_000_000)
        assert summary.total_risk_amount == pytest.approx(1.0, abs=0.01)

    def test_risk_scales_with_lot_size(self):
        one = compute_open_risk([_pos(1, "E", 1.0, 1.1, 1.099)],
                                {"E": _info()}, 10_000)
        four = compute_open_risk([_pos(1, "E", 4.0, 1.1, 1.099)],
                                 {"E": _info()}, 10_000)
        assert four.total_risk_amount == pytest.approx(4 * one.total_risk_amount)

    def test_risk_is_independent_of_profit(self):
        """A profitable position can still carry risk; P&L is not risk."""
        positions = [_pos(1, "E", 1.0, 1.1, 1.099, profit=+500.0)]
        summary = compute_open_risk(positions, {"E": _info()}, 1_000_000)
        assert summary.total_risk_amount == pytest.approx(100.0)

    def test_wider_stop_means_more_risk(self):
        tight = compute_open_risk([_pos(1, "E", 1.0, 1.1, 1.0990)],
                                  {"E": _info()}, 1_000_000)
        wide = compute_open_risk([_pos(1, "E", 1.0, 1.1, 1.0900)],
                                 {"E": _info()}, 1_000_000)
        assert wide.total_risk_amount > tight.total_risk_amount

    def test_stop_above_entry_for_a_short(self):
        """Distance is absolute, so a short's SL above entry still counts."""
        long_pos = compute_open_risk([_pos(1, "E", 1.0, 1.1000, 1.0990)],
                                     {"E": _info()}, 1_000_000)
        short_pos = compute_open_risk([_pos(2, "E", 1.0, 1.1000, 1.1010)],
                                      {"E": _info()}, 1_000_000)
        assert long_pos.total_risk_amount == pytest.approx(
            short_pos.total_risk_amount)

    def test_percentage_of_equity(self):
        # 100 risk on 10000 equity = 1%
        positions = [_pos(1, "E", 1.0, 1.1, 1.099)]
        summary = compute_open_risk(positions, {"E": _info()}, equity=10_000)
        assert summary.risk_pct_of_equity == pytest.approx(1.0)
        assert summary.risk_pct_display() == "1.00%"

    def test_percentage_is_none_without_equity(self):
        summary = compute_open_risk([_pos(1, "E", 1.0, 1.1, 1.099)],
                                    {"E": _info()}, equity=0)
        assert summary.risk_pct_of_equity is None
        assert summary.risk_pct_display() == "N/A"


class TestUnprotectedPositions:

    def test_position_without_sl_is_unprotected(self):
        positions = [_pos(1, "E", 1.0, 1.1, 0.0)]  # no stop
        summary = compute_open_risk(positions, {"E": _info()}, 10_000)
        assert summary.has_unprotected is True
        assert summary.unprotected_count == 1
        assert summary.positions[0].status == "UNPROTECTED"

    def test_position_with_sl_is_protected(self):
        summary = compute_open_risk([_pos(1, "E", 1.0, 1.1, 1.099)],
                                    {"E": _info()}, 10_000)
        assert summary.has_unprotected is False
        assert summary.positions[0].status == "PROTECTED"

    def test_unprotected_is_counted_separately_not_as_zero_risk(self):
        """The whole point: an unprotected position has unknown risk.

        Folding it into the total as 0.0 would understate exposure, which is
        the dangerous direction of error.
        """
        positions = [_pos(1, "E", 1.0, 1.1, 1.099),   # protected, risk 100
                     _pos(2, "F", 1.0, 2000.0, 0.0)]   # unprotected
        summary = compute_open_risk(positions, {"E": _info(), "F": _info()}, 1_000_000)
        assert summary.total_risk_amount == pytest.approx(100.0)
        assert summary.unprotected_count == 1
        assert summary.risk_is_partial is True, (
            "a partial risk figure must be flagged as partial"
        )

    def test_fully_protected_book_is_not_partial(self):
        summary = compute_open_risk([_pos(1, "E", 1.0, 1.1, 1.099)],
                                    {"E": _info()}, 10_000)
        assert summary.risk_is_partial is False

    def test_missing_symbol_info_leaves_risk_unknown(self):
        """No tick value means risk cannot be computed, so it is not 0."""
        summary = compute_open_risk([_pos(1, "E", 1.0, 1.1, 1.099)],
                                    {}, 10_000)
        assert summary.has_unprotected is True
        assert summary.risk_is_partial is True

    def test_zero_tick_value_leaves_risk_unknown(self):
        summary = compute_open_risk([_pos(1, "E", 1.0, 1.1, 1.099)],
                                    {"E": _info(tick_value=0.0)}, 10_000)
        assert summary.risk_is_partial is True


class TestRiskColour:

    def test_unprotected_positions_are_amber(self):
        """Calm green would understate an unknown worst case."""
        summary = compute_open_risk([_pos(1, "E", 1.0, 1.1, 0.0)],
                                    {"E": _info()}, 1_000_000)
        assert format_risk_colour(summary) == "#F59E0B"

    def test_high_risk_is_red(self):
        # 300 risk on 1000 equity = 30%
        summary = compute_open_risk([_pos(1, "E", 1.0, 1.1, 1.097)],
                                    {"E": _info()}, 1000)
        assert format_risk_colour(summary) == "#F43F5E"

    def test_moderate_risk_is_amber(self):
        # 30 risk on 1000 equity = 3%
        summary = compute_open_risk([_pos(1, "E", 1.0, 1.1, 1.0997)],
                                    {"E": _info()}, 1000)
        assert summary.risk_pct_of_equity == pytest.approx(3.0, abs=0.1)
        assert format_risk_colour(summary) == "#FBBF24"

    def test_low_risk_is_green(self):
        # 100 risk on 100000 equity = 0.1%
        summary = compute_open_risk([_pos(1, "E", 1.0, 1.1, 1.099)],
                                    {"E": _info()}, 100_000)
        assert format_risk_colour(summary) == "#34D399"

    def test_no_equity_is_slate_not_green(self):
        summary = compute_open_risk([_pos(1, "E", 1.0, 1.1, 1.099)],
                                    {"E": _info()}, 0)
        assert format_risk_colour(summary) == "#94A3B8"


# ─────────────────────────────────────────────────────────────────────────────
# LLM confidence presentation
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatLlmConfidence:

    def test_hold_shows_no_score(self):
        """A safety refusal carries no confidence in acting."""
        assert format_llm_confidence(60, "HOLD") == NO_SCORE
        assert format_llm_confidence(85, "HOLD") == NO_SCORE
        assert format_llm_confidence(0, "HOLD") == NO_SCORE

    def test_hold_ignores_any_numeric_value(self):
        """Even a real number is not rendered on a HOLD."""
        for value in (0, 1, 50, 99, 100):
            assert format_llm_confidence(value, "HOLD") == NO_SCORE

    def test_lowercase_hold_is_still_no_score(self):
        assert format_llm_confidence(60, "hold") == NO_SCORE

    def test_buy_and_sell_render_the_number(self):
        assert format_llm_confidence(85, "BUY") == "85%"
        assert format_llm_confidence(72, "SELL") == "72%"

    def test_none_renders_no_score(self):
        assert format_llm_confidence(None, "BUY") == NO_SCORE
        assert format_llm_confidence(None, None) == NO_SCORE

    def test_low_confidence_is_not_raised(self):
        """No floor: a 12 stays a 12."""
        assert format_llm_confidence(12, "BUY") == "12%"

    def test_non_numeric_renders_no_score(self):
        assert format_llm_confidence("high", "BUY") == NO_SCORE
        assert format_llm_confidence([], "BUY") == NO_SCORE
        assert format_llm_confidence({}, "BUY") == NO_SCORE

    def test_nan_and_inf_render_no_score(self):
        assert format_llm_confidence(math.nan, "BUY") == NO_SCORE
        assert format_llm_confidence(math.inf, "BUY") == NO_SCORE

    def test_bool_is_not_a_confidence(self):
        assert format_llm_confidence(True, "BUY") == NO_SCORE

    def test_zero_renders_as_zero_percent(self):
        assert format_llm_confidence(0, "BUY") == "0%"

    def test_no_score_marker_is_explicit_text(self):
        assert NO_SCORE == "n/a"


class TestHasConfidenceScore:

    def test_real_number_has_a_score(self):
        assert metrics.has_confidence_score(85) is True
        assert metrics.has_confidence_score(0) is True

    def test_missing_score_is_detected(self):
        assert metrics.has_confidence_score(None) is False
        assert metrics.has_confidence_score("85") is False
        assert metrics.has_confidence_score(math.nan) is False
        assert metrics.has_confidence_score(math.inf) is False
        assert metrics.has_confidence_score(True) is False


class TestSafeHoldCarriesNoConfidence:
    """F1.2: the fail-safe template must not carry a fabricated score."""

    def test_safe_hold_confidence_is_none(self):
        from ai_brain import _safe_hold
        assert _safe_hold("boom")["confidence"] is None

    def test_safe_hold_renders_as_no_score(self):
        from ai_brain import _safe_hold
        result = _safe_hold("boom")
        assert format_llm_confidence(result["confidence"], result["decision"]) == NO_SCORE

    def test_every_fail_safe_path_has_no_confidence(self):
        """Each failure mode must return a HOLD with no numeric score."""
        import types
        import pandas as pd
        import requests
        import config as cfg

        from ai_brain import get_ai_decision

        def df():
            return pd.DataFrame({
                "close": [1.1], "ema20": [1.099], "ema50": [1.097],
                "rsi": [55.0], "macd_hist": [0.0005], "atr": [0.0008],
            })

        cases = []

        # No API key
        orig_key = cfg.OPENROUTER_API_KEY
        cfg.OPENROUTER_API_KEY = "YOUR_KEY_HERE"
        cases.append(("no api key", get_ai_decision(df(), df())))
        cfg.OPENROUTER_API_KEY = orig_key

        # No data
        cases.append(("no data", get_ai_decision(None, None)))

        # Transport failure
        orig_post = requests.post
        requests.post = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("x"))
        cases.append(("timeout", get_ai_decision(df(), df())))
        requests.post = orig_post

        # Malformed JSON
        requests.post = lambda *a, **k: types.SimpleNamespace(
            status_code=200, json=lambda: {"choices": [{"message": {"content": "nope"}}]})
        cases.append(("bad json", get_ai_decision(df(), df())))
        requests.post = orig_post

        # Hallucination
        requests.post = lambda *a, **k: types.SimpleNamespace(
            status_code=200, json=lambda: {"choices": [{"message": {"content":
                '{"decision":"BUY","confidence":90,"strength":"STRONG",'
                '"reasoning":"atr is 9.99999","risk_reward":"1:2"}'}}]})
        cases.append(("hallucination", get_ai_decision(df(), df())))
        requests.post = orig_post

        for label, result in cases:
            assert result["decision"] == "HOLD", f"{label}: {result['decision']}"
            assert result["confidence"] is None, (
                f"{label}: fail-safe produced confidence {result['confidence']!r}"
            )