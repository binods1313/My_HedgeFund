"""LLM validation layer tests.

Covers:
  - apply_llm_adjustment(): the executable rule. The LLM may only reduce
    confidence or reject; it can never raise a score above the threshold, and
    the threshold is applied strictly after the adjustment.
  - build_indicator_snapshot(): None / empty / short / NaN / missing-column
    guards, and that it contains computed values only.
  - _detect_hallucination(): per-field tolerances read from config.yaml, with
    every cited number checked against every snapshot field.
  - get_ai_decision(): fail-safe HOLD on timeout / bad JSON / schema violation /
    hallucination / missing data, and no confidence floor.

Run: python -m pytest tests/test_llm_validator.py -v
"""
import json
import types

import pandas as pd
import pytest

from ai_brain import (
    ADJUST_MAX,
    ADJUST_MIN,
    _detect_hallucination,
    _safe_hold,
    apply_llm_adjustment,
    build_indicator_snapshot,
)
from config_loader import LlmTolerances


def _make_df(close=1.10000, ema20=1.09900, ema50=1.09700,
             rsi=55.0, macd_hist=0.00050, atr=0.00080) -> pd.DataFrame:
    """Minimal 2-row DataFrame with the indicator columns."""
    return pd.DataFrame({
        "close": [close - 0.0001, close],
        "ema20": [ema20 - 0.0001, ema20],
        "ema50": [ema50 - 0.0001, ema50],
        "rsi": [rsi - 1, rsi],
        "macd_hist": [macd_hist - 0.0001, macd_hist],
        "atr": [atr, atr],
    })


def _make_snapshot(close=1.10000, ema20=1.09900, ema50=1.09700, rsi=55.0,
                   macd_hist=0.00050, atr=0.00080) -> dict:
    """Snapshot fixture.

    h4 values are derived from the m15 inputs so that per-field tolerance tests
    are not accidentally satisfied by a different frame's value. h4.rsi equals
    m15.rsi deliberately: to isolate RSI tolerance, the two frames must not
    offer competing candidate values.
    """
    return {
        "m15": {"close": close, "ema20": ema20, "ema50": ema50, "rsi": rsi,
                "macd_hist": macd_hist, "atr": atr},
        "h4": {"close": close + 0.001, "ema50": ema50 - 0.001, "rsi": rsi},
    }


def _fake_llm_response(monkeypatch, content: str, status_code: int = 200):
    """Point requests.post at a canned OpenRouter-style response."""
    import requests
    import config as cfg
    monkeypatch.setattr(cfg, "OPENROUTER_API_KEY", "sk-test-real-key")
    fake_msg = {"choices": [{"message": {"content": content}}]}
    monkeypatch.setattr(
        requests, "post",
        lambda *a, **kw: types.SimpleNamespace(status_code=status_code,
                                               json=lambda: fake_msg),
    )


def _llm_payload(**overrides) -> str:
    base = {
        "decision": "BUY",
        "confidence": 85,
        "strength": "STRONG",
        "reasoning": "Price 1.10000 above EMA20 1.09900.",
        "risk_reward": "1:2",
    }
    base.update(overrides)
    return json.dumps(base)


# ─────────────────────────────────────────────────────────────────────────────
# T1: apply_llm_adjustment — the executable rule
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyLlmAdjustmentCannotRaise:

    @pytest.mark.parametrize("adjust", [0, -1, -5, -10, -19, -20])
    def test_final_never_exceeds_signal_score(self, adjust):
        """For every valid adjust in [-20..0], final <= signal_score."""
        signal_score = 90.0
        result = apply_llm_adjustment(signal_score, "APPROVE", adjust, threshold=85.0)
        assert result.final_score <= signal_score, (
            f"final {result.final_score} exceeded signal {signal_score} "
            f"with adjust {adjust}"
        )
        assert result.final_score == pytest.approx(signal_score + adjust)

    def test_positive_adjust_is_rejected(self):
        """The LLM attempting to RAISE confidence must be a hard stop."""
        result = apply_llm_adjustment(70.0, "APPROVE", +5, threshold=85.0)
        assert result.decision == "NO_TRADE"
        assert result.final_score == 70.0
        assert "only reduce" in result.reason

    def test_below_min_adjust_is_rejected(self):
        result = apply_llm_adjustment(90.0, "APPROVE", -20.5, threshold=85.0)
        assert result.decision == "NO_TRADE"
        assert result.final_score == 90.0
        assert "outside allowed range" in result.reason

    @pytest.mark.parametrize("bad", [None, "5", "abc", [], {}, object()])
    def test_non_numeric_adjust_is_rejected(self, bad):
        result = apply_llm_adjustment(90.0, "APPROVE", bad, threshold=85.0)
        assert result.decision == "NO_TRADE"
        assert result.final_score == 90.0

    def test_bool_adjust_is_rejected(self):
        """bool is an int subclass; True must not be read as an adjustment."""
        result = apply_llm_adjustment(90.0, "APPROVE", True, threshold=85.0)
        assert result.decision == "NO_TRADE"

    def test_nan_adjust_is_rejected(self):
        result = apply_llm_adjustment(90.0, "APPROVE", float("nan"), threshold=85.0)
        assert result.decision == "NO_TRADE"

    @pytest.mark.parametrize("verdict", ["MAYBE", "", "APPROVED", None, 42])
    def test_invalid_verdict_is_rejected(self, verdict):
        result = apply_llm_adjustment(90.0, verdict, 0, threshold=85.0)
        assert result.decision == "NO_TRADE"
        assert "Invalid LLM verdict" in result.reason

    def test_reject_verdict_gives_no_trade(self):
        """REJECT is a hard stop even with a signal far above threshold."""
        result = apply_llm_adjustment(99.0, "REJECT", 0, threshold=50.0)
        assert result.decision == "NO_TRADE"
        assert result.final_score == 99.0
        assert result.reason == "LLM rejected the signal"

    def test_llm_cannot_lift_subthreshold_signal(self):
        """score 70 + adjust 0 + threshold 85 => no trade, and adjust may not help."""
        result = apply_llm_adjustment(70.0, "APPROVE", 0, threshold=85.0)
        assert result.decision == "NO_TRADE"
        assert result.final_score == 70.0
        assert result.final_score <= 70.0


class TestThresholdAppliedAfterAdjustment:

    def test_86_minus_2_trades(self):
        """86 - 2 = 84 ... which is NOT > 85, so this must NOT trade.

        Documented deliberately: this is the case that proves the threshold is
        evaluated on the post-adjustment score rather than the raw signal score.
        """
        result = apply_llm_adjustment(86.0, "REDUCE", -2, threshold=85.0)
        assert result.final_score == pytest.approx(84.0)
        assert result.decision == "NO_TRADE"

    def test_88_minus_2_trades(self):
        """88 - 2 = 86 > 85 -> TRADE."""
        result = apply_llm_adjustment(88.0, "REDUCE", -2, threshold=85.0)
        assert result.final_score == pytest.approx(86.0)
        assert result.decision == "TRADE"

    def test_86_minus_5_does_not_trade(self):
        """86 - 5 = 81, below threshold -> NO_TRADE."""
        result = apply_llm_adjustment(86.0, "REDUCE", -5, threshold=85.0)
        assert result.final_score == pytest.approx(81.0)
        assert result.decision == "NO_TRADE"

    def test_score_exactly_at_threshold_does_not_trade(self):
        """Threshold is strictly greater-than, so 85 == 85 must not trade."""
        result = apply_llm_adjustment(85.0, "APPROVE", 0, threshold=85.0)
        assert result.final_score == pytest.approx(85.0)
        assert result.decision == "NO_TRADE"

    def test_score_one_above_threshold_trades(self):
        result = apply_llm_adjustment(85.5, "APPROVE", 0, threshold=85.0)
        assert result.decision == "TRADE"


class TestLegacyConfidenceHasNoFloor:
    """The legacy display path must report the model's number unchanged."""

    def test_low_confidence_is_not_floored(self, monkeypatch):
        """An LLM answer of 12 must come back as 12, not lifted to 60."""
        _fake_llm_response(monkeypatch, _llm_payload(confidence=12))
        from ai_brain import get_ai_decision
        result = get_ai_decision(df_m15=_make_df(), df_h4=_make_df())
        assert result["confidence"] == 12, (
            f"legacy confidence was altered to {result['confidence']}"
        )

    def test_zero_confidence_is_not_floored(self, monkeypatch):
        _fake_llm_response(monkeypatch, _llm_payload(confidence=0))
        from ai_brain import get_ai_decision
        result = get_ai_decision(df_m15=_make_df(), df_h4=_make_df())
        assert result["confidence"] == 0

    def test_high_confidence_is_not_capped_at_95(self, monkeypatch):
        """No upward or downward clamping: 99 comes back as 99."""
        _fake_llm_response(monkeypatch, _llm_payload(confidence=99))
        from ai_brain import get_ai_decision
        result = get_ai_decision(df_m15=_make_df(), df_h4=_make_df())
        assert result["confidence"] == 99

    def test_adjust_bounds_are_minus_twenty_to_zero(self):
        assert (ADJUST_MIN, ADJUST_MAX) == (-20.0, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# build_indicator_snapshot guards
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildIndicatorSnapshot:

    def test_returns_correct_keys(self):
        snap = build_indicator_snapshot(_make_df(), _make_df())
        assert set(snap.keys()) == {"m15", "h4"}
        assert set(snap["m15"]) == {"close", "ema20", "ema50", "rsi", "macd_hist", "atr"}
        assert set(snap["h4"]) == {"close", "ema50", "rsi"}

    def test_values_match_last_row(self):
        df = _make_df(close=1.23456, ema20=1.23000, rsi=62.5)
        snap = build_indicator_snapshot(df, df)
        assert snap["m15"]["close"] == pytest.approx(1.23456, rel=1e-4)
        assert snap["m15"]["ema20"] == pytest.approx(1.23000, rel=1e-4)
        assert snap["m15"]["rsi"] == pytest.approx(62.5, rel=1e-4)

    def test_none_returns_empty(self):
        assert build_indicator_snapshot(None, None) == {}

    def test_one_none_returns_empty(self):
        assert build_indicator_snapshot(_make_df(), None) == {}
        assert build_indicator_snapshot(None, _make_df()) == {}

    def test_empty_returns_empty(self):
        assert build_indicator_snapshot(pd.DataFrame(), pd.DataFrame()) == {}

    def test_missing_indicator_column_returns_empty(self):
        df = _make_df().drop(columns=["atr"])
        assert build_indicator_snapshot(df, _make_df()) == {}

    def test_nan_indicator_returns_empty(self):
        df = _make_df()
        df.loc[df.index[-1], "rsi"] = float("nan")
        assert build_indicator_snapshot(df, _make_df()) == {}

    def test_values_are_rounded(self):
        snap = build_indicator_snapshot(_make_df(close=1.123456789), _make_df())
        assert len(str(snap["m15"]["close"]).split(".")[-1]) <= 5


# ─────────────────────────────────────────────────────────────────────────────
# T2: per-field hallucination tolerances
# ─────────────────────────────────────────────────────────────────────────────

class TestHallucinationPerField:

    # -- price (0.2% relative) --
    def test_price_inside_tolerance_passes(self):
        snap = _make_snapshot(close=1.10000)
        cited = 1.10000 * (1 + 0.001)  # 0.1% < 0.2%
        assert _detect_hallucination({"reasoning": f"close {cited:.5f}"}, snap) is None

    def test_price_outside_tolerance_is_hallucination(self):
        snap = _make_snapshot(close=1.10000)
        cited = 1.10000 * (1 + 0.005)  # 0.5% > 0.2%
        err = _detect_hallucination({"reasoning": f"close {cited:.5f}"}, snap)
        assert err is not None and "Hallucination" in err

    # -- EMA (0.2% relative) --
    def test_ema_inside_tolerance_passes(self):
        snap = _make_snapshot(ema20=1.09900)
        cited = 1.09900 * (1 + 0.001)
        assert _detect_hallucination({"reasoning": f"ema20 {cited:.5f}"}, snap) is None

    def test_ema_outside_tolerance_is_hallucination(self):
        snap = _make_snapshot(ema20=1.09900)
        cited = 1.09900 * (1 + 0.01)
        err = _detect_hallucination({"reasoning": f"ema20 {cited:.5f}"}, snap)
        assert err is not None and "Hallucination" in err

    def test_ema50_outside_tolerance_is_hallucination(self):
        snap = _make_snapshot(ema50=1.09700)
        cited = 1.09700 * (1 + 0.01)
        err = _detect_hallucination({"reasoning": f"ema50 {cited:.5f}"}, snap)
        assert err is not None and "Hallucination" in err

    # -- ATR (5% relative) --
    def test_atr_inside_tolerance_passes(self):
        snap = _make_snapshot(atr=0.00080)
        cited = 0.00080 * (1 + 0.02)  # 2% < 5%
        assert _detect_hallucination({"reasoning": f"atr {cited:.5f}"}, snap) is None

    def test_atr_outside_tolerance_is_hallucination(self):
        snap = _make_snapshot(atr=0.00080)
        cited = 0.00080 * (1 + 0.30)  # 30% > 5%
        err = _detect_hallucination({"reasoning": f"atr {cited:.5f}"}, snap)
        assert err is not None and "Hallucination" in err

    # -- RSI (2 points absolute) --
    def test_rsi_inside_tolerance_passes(self):
        snap = _make_snapshot(rsi=55.0)
        assert _detect_hallucination({"reasoning": "rsi 56.5"}, snap) is None

    def test_rsi_outside_tolerance_is_hallucination(self):
        snap = _make_snapshot(rsi=55.0)
        err = _detect_hallucination({"reasoning": "rsi 70"}, snap)
        assert err is not None and "Hallucination" in err

    def test_rsi_is_absolute_not_relative(self):
        """3 points off must fail even though that is only ~5% of 55."""
        snap = _make_snapshot(rsi=55.0)
        assert _detect_hallucination({"reasoning": "rsi 58.0"}, snap) is not None

    # -- MACD (10% relative with absolute floor) --
    def test_macd_inside_tolerance_passes(self):
        snap = _make_snapshot(macd_hist=0.00050)
        cited = 0.00050 * (1 + 0.05)  # 5% < 10%
        assert _detect_hallucination({"reasoning": f"macd {cited:.5f}"}, snap) is None

    def test_macd_outside_tolerance_is_hallucination(self):
        snap = _make_snapshot(macd_hist=0.00050)
        cited = 0.00050 * (1 + 0.90)  # 90% > 10%
        err = _detect_hallucination({"reasoning": f"macd {cited:.5f}"}, snap)
        assert err is not None and "Hallucination" in err

    def test_every_cited_number_is_checked(self):
        """A single bad number anywhere in the reasoning is enough to reject."""
        snap = _make_snapshot(close=1.10000)
        reasoning = "close 1.10000 and ema20 1.09900 but rsi 99"
        err = _detect_hallucination({"reasoning": reasoning}, snap)
        assert err is not None and "Hallucination" in err

    def test_no_numbers_cited_passes(self):
        snap = _make_snapshot()
        assert _detect_hallucination({"reasoning": "trend is bullish"}, snap) is None

    def test_wildly_out_of_range_price_is_hallucination(self):
        """The old 50%-150% ballpark check missed this; per-field must not."""
        snap = _make_snapshot(close=1.10000)
        err = _detect_hallucination({"reasoning": "close is 37.83"}, snap)
        assert err is not None and "Hallucination" in err


class TestTolerancesAreWiredToConfig:
    """Proves the tolerances actually come from config, not a constant."""

    def _tols(self, **overrides) -> LlmTolerances:
        base = dict(price=0.002, ema=0.002, atr=0.05, rsi_points=2.0,
                    macd=0.10, macd_abs_floor=1e-05)
        base.update(overrides)
        return LlmTolerances(**base)

    def test_config_default_matches_config_yaml(self):
        from config_loader import load_config
        tol = load_config().llm.hallucination_tolerances
        assert tol.price == 0.002
        assert tol.ema == 0.002
        assert tol.atr == 0.05
        assert tol.rsi_points == 2.0
        assert tol.macd == 0.10

    def test_loosening_price_tolerance_changes_verdict(self, monkeypatch):
        """A 0.5% price error is rejected at 0.2% but accepted at 1%."""
        snap = _make_snapshot(close=1.10000)
        cited = 1.10000 * (1 + 0.005)
        parsed = {"reasoning": f"close {cited:.5f}"}

        strict = _detect_hallucination(parsed, snap, self._tols(price=0.002))
        assert strict is not None

        loose = _detect_hallucination(parsed, snap, self._tols(price=0.010))
        assert loose is None

    def test_tightening_rsi_tolerance_changes_verdict(self):
        snap = _make_snapshot(rsi=55.0)
        parsed = {"reasoning": "rsi 57.0"}

        assert _detect_hallucination(parsed, snap, self._tols(rsi_points=2.0)) is None
        assert _detect_hallucination(parsed, snap, self._tols(rsi_points=1.0)) is not None

    def test_detect_uses_loaded_config_by_default(self, monkeypatch):
        """With no explicit tolerance object, the config value governs."""
        snap = _make_snapshot(close=1.10000)
        cited = 1.10000 * (1 + 0.005)
        parsed = {"reasoning": f"close {cited:.5f}"}

        import ai_brain

        monkeypatch.setattr(ai_brain, "load_config", lambda: types.SimpleNamespace(
            llm=types.SimpleNamespace(hallucination_tolerances=self._tols(price=0.002))))
        assert _detect_hallucination(parsed, snap) is not None

        monkeypatch.setattr(ai_brain, "load_config", lambda: types.SimpleNamespace(
            llm=types.SimpleNamespace(hallucination_tolerances=self._tols(price=0.010))))
        assert _detect_hallucination(parsed, snap) is None


# ─────────────────────────────────────────────────────────────────────────────
# get_ai_decision — fail-safe paths
# ─────────────────────────────────────────────────────────────────────────────

class TestGetAiDecisionFailSafe:
    """Every failure mode must return decision=HOLD, never BUY/SELL."""

    def test_missing_api_key_returns_hold(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "OPENROUTER_API_KEY", "YOUR_KEY_HERE")
        from ai_brain import get_ai_decision
        assert get_ai_decision(df_m15=_make_df(), df_h4=_make_df())["decision"] == "HOLD"

    def test_none_dataframes_returns_hold(self, monkeypatch):
        _fake_llm_response(monkeypatch, _llm_payload())
        from ai_brain import get_ai_decision
        assert get_ai_decision(df_m15=None, df_h4=None)["decision"] == "HOLD"

    def test_empty_dataframes_returns_hold(self, monkeypatch):
        _fake_llm_response(monkeypatch, _llm_payload())
        from ai_brain import get_ai_decision
        assert get_ai_decision(df_m15=pd.DataFrame(), df_h4=pd.DataFrame())["decision"] == "HOLD"

    def test_api_timeout_returns_hold(self, monkeypatch):
        import config as cfg
        import requests
        monkeypatch.setattr(cfg, "OPENROUTER_API_KEY", "sk-test-real-key")
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("timed out")),
        )
        from ai_brain import get_ai_decision
        assert get_ai_decision(df_m15=_make_df(), df_h4=_make_df())["decision"] == "HOLD"

    def test_api_error_status_returns_hold(self, monkeypatch):
        import config as cfg
        import requests
        monkeypatch.setattr(cfg, "OPENROUTER_API_KEY", "sk-test-real-key")
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **kw: types.SimpleNamespace(status_code=500, json=dict),
        )
        from ai_brain import get_ai_decision
        assert get_ai_decision(df_m15=_make_df(), df_h4=_make_df())["decision"] == "HOLD"

    def test_malformed_json_returns_hold(self, monkeypatch):
        _fake_llm_response(monkeypatch, "not json at all")
        from ai_brain import get_ai_decision
        assert get_ai_decision(df_m15=_make_df(), df_h4=_make_df())["decision"] == "HOLD"

    def test_json_array_response_returns_hold(self, monkeypatch):
        _fake_llm_response(monkeypatch, "[1, 2, 3]")
        from ai_brain import get_ai_decision
        assert get_ai_decision(df_m15=_make_df(), df_h4=_make_df())["decision"] == "HOLD"

    def test_invalid_decision_field_returns_hold(self, monkeypatch):
        _fake_llm_response(monkeypatch, _llm_payload(decision="MAYBE"))
        from ai_brain import get_ai_decision
        assert get_ai_decision(df_m15=_make_df(), df_h4=_make_df())["decision"] == "HOLD"

    def test_out_of_range_confidence_returns_hold(self, monkeypatch):
        """A stringified confidence is not a number -> HOLD, not silent reuse."""
        _fake_llm_response(monkeypatch, _llm_payload(confidence="very high"))
        from ai_brain import get_ai_decision
        result = get_ai_decision(df_m15=_make_df(), df_h4=_make_df())
        assert result["decision"] == "HOLD"

    def test_hallucinated_price_returns_hold(self, monkeypatch):
        _fake_llm_response(monkeypatch, _llm_payload(
            reasoning="Price at 1.05000 is far below EMA20."))
        from ai_brain import get_ai_decision
        result = get_ai_decision(df_m15=_make_df(close=1.10000), df_h4=_make_df())
        assert result["decision"] == "HOLD"
        assert result.get("hallucination_detected") is True

    def test_accurate_citation_is_accepted(self, monkeypatch):
        """Guards against over-rejection: exact values must pass."""
        _fake_llm_response(monkeypatch, _llm_payload())
        from ai_brain import get_ai_decision
        result = get_ai_decision(df_m15=_make_df(), df_h4=_make_df())
        assert result["decision"] == "BUY"
        assert result["hallucination_detected"] is False

    def test_safe_hold_never_raises(self):
        assert _safe_hold("boom")["decision"] == "HOLD"
        assert _safe_hold("boom", {"m15": {}})["indicator_snapshot"] == {"m15": {}}