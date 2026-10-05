r"""Tests for the dynamic anchors, the xAI gate, sizing and the live guard.

Covers the four technical specifications:

  1. Fibonacci anchors confirmed by a sweep and by ATR, with an immutable OTE
     band and 100% invalidation.
  2. The xAI gate, which may only remove a setup. Below threshold it is never
     consulted; above it, a rejection kills the intent.
  3. Lot sizing that fails closed below the broker minimum.
  4. A live invalidation re-check immediately before dispatch.
"""
import numpy as np
import pandas as pd
import pytest

from engine.executor import ExecutionRejected, LiveGuard, PaperExecutor, RiskApproval
from engine.indicators.structure import (
    MIN_LEG_ATR_MULTIPLE,
    OTE_HIGH,
    OTE_LOW,
    SwingLeg,
    in_ote_zone,
    ote_zone,
    select_structural_leg,
)
from engine.llm_client import ProviderTimeout, Validation, Verdict
from engine.sizing import LOTS_BELOW_MINIMUM_LOG, LotSizer
from engine.validation import (
    MIN_CONFLUENCES_FOR_XAI,
    PROVIDER_FAILURE_LOG,
    REJECTION_LOG,
    SpatialContext,
    XAIValidationGate,
)

from tests.test_engine.test_indicators import flat, frame
from tests.test_strategy_fixtures import EURUSD_SPEC, make_spec


def context() -> SpatialContext:
    return SpatialContext(
        symbol="EURUSD", timeframe="M15", fvg_size=0.00065,
        sweep_type="BUY", ote_distance=0.0021, trend_bias="BULLISH")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Dynamic anchors
# ─────────────────────────────────────────────────────────────────────────────

class TestStructuralLegAnchors:

    def test_leg_requires_a_swept_low_for_a_bullish_setup(self):
        """No sweep, no leg: an unanchored swing is exactly what this prevents."""
        rows = [(100 + i * 0.01, 100.2 + i * 0.01, 99.8 + i * 0.01, 100 + i * 0.01)
                for i in range(40)]
        df = frame(rows)
        assert select_structural_leg(df, sweep=None, atr=0.01) is None, (
            "a leg must not be built without a liquidity sweep to anchor it"
        )

    def test_leg_is_returned_when_a_low_was_swept(self):
        """A swept low, then a reversal, anchored on the window low.

        The leg anchors on the window low (99.5) rather than the swept level
        (99.9): a sweep commonly continues before reversing, and that extension
        is the structure being traded.
        """
        rows = [(100.0, 100.2, 99.9, 100.0)] * 20
        rows += [(100.0, 100.1, 99.5, 100.0)]          # sweeps the 99.9 low
        rows += [(100.0, 100.5 + i * 0.01, 99.95, 100.3 + i * 0.01)
                 for i in range(5)]
        df = frame(rows)

        from engine.indicators.structure import Sweep
        sweep = Sweep(index=20, timestamp=None, direction="BUY",
                      level=99.9, extreme=0.4, reclaimed=True)

        leg = select_structural_leg(df, sweep=sweep, lookback_bars=20, atr=0.01)
        assert leg is not None, "a swept low followed by a reversal must anchor"
        assert leg.direction == "BULLISH"
        assert leg.invalidation == pytest.approx(99.5), (
            "the 100% invalidation is the window low, not the swept level"
        )

    def test_leg_must_span_three_atr(self):
        """A 'swing' inside normal volatility is noise."""
        rows = [(100.0, 100.1, 99.95, 100.0)] * 15
        rows += [(100.0, 100.11, 99.99, 100.05)]        # a 0.02 wiggle
        rows += [(100.05, 100.2, 100.04, 100.15)] * 5
        df = frame(rows)

        from engine.indicators.structure import Sweep
        sweep = Sweep(index=15, timestamp=None, direction="BUY",
                      level=99.95, extreme=0.04, reclaimed=True)

        atr = 0.10
        leg = select_structural_leg(df, sweep=sweep, lookback_bars=20, atr=atr)
        assert leg is None, (
            f"a leg spanning less than {MIN_LEG_ATR_MULTIPLE} x ATR must be "
            f"rejected; span was "
            f"{abs(leg.end_price - leg.start_price):g}" if leg else ""
        )

    def test_short_history_yields_no_leg(self):
        assert select_structural_leg(flat(10), sweep=None) is None


class TestInvalidation:

    def _bull_leg(self):
        return SwingLeg(start_index=0, end_index=10, start_price=1.0900,
                        end_price=1.1000, direction="BULLISH",
                        invalidation=1.0900)

    def test_price_below_the_swing_low_invalidates_a_long(self):
        leg = self._bull_leg()
        assert leg.is_invalidated(1.0899) is True
        assert leg.is_invalidated(1.0901) is False

    def test_price_above_the_swing_high_invalidates_a_short(self):
        leg = SwingLeg(start_index=0, end_index=10, start_price=1.1000,
                       end_price=1.0900, direction="BEARISH",
                       invalidation=1.1000)
        assert leg.is_invalidated(1.1001) is True
        assert leg.is_invalidated(1.0999) is False

    def test_no_invalidation_level_means_never_invalid(self):
        leg = SwingLeg(0, 10, 1.09, 1.10, "BULLISH", invalidation=0.0)
        assert leg.is_invalidated(0.5) is False


class TestOteBandIsImmutable:

    def test_band_is_the_documented_retracement_range(self):
        leg = SwingLeg(0, 10, 1.0000, 1.1000, "BEARISH", invalidation=1.1000)
        lower, upper = ote_zone(leg)
        span = abs(leg.end_price - leg.start_price)
        assert (leg.end_price - upper) / span == pytest.approx(OTE_LOW, abs=1e-6)
        assert (leg.end_price - lower) / span == pytest.approx(OTE_HIGH, abs=1e-6)

    def test_band_never_moves_for_the_same_leg(self):
        """An immutable boundary: the same leg yields the same band every read."""
        leg = SwingLeg(0, 10, 1.0000, 1.1000, "BULLISH", invalidation=1.0000)
        first = ote_zone(leg)
        for _ in range(5):
            assert ote_zone(leg) == first

    def test_price_outside_the_band_is_outside_ote(self):
        leg = SwingLeg(0, 10, 1.0000, 1.1000, "BEARISH", invalidation=1.1000)
        lower, upper = ote_zone(leg)
        assert in_ote_zone((lower + upper) / 2, leg) is True
        assert in_ote_zone(upper + 1.0, leg) is False


# ─────────────────────────────────────────────────────────────────────────────
# 2. xAI gate
# ─────────────────────────────────────────────────────────────────────────────

class FakeClient:
    """Stands in for XAIClient with a scripted outcome."""

    def __init__(self, *, configured=True, result=None, raises=None):
        self._configured = configured
        self._result = result
        self._raises = raises
        self.calls = 0

    def is_configured(self):
        return self._configured

    def validate_trade(self, snapshot):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._result

    def describe(self):
        return "fake"


def approve() -> Validation:
    return Validation(verdict=Verdict.APPROVE, confidence_adjust=0.0,
                      rationale="structure valid")


def reject() -> Validation:
    return Validation(verdict=Verdict.REJECT, confidence_adjust=-20.0,
                      rationale="spatial constraints invalid")


def reduce_conf() -> Validation:
    return Validation(verdict=Verdict.REDUCE, confidence_adjust=-6.0,
                      rationale="weak structure")


class TestXaiGateIsAsymmetric:

    def test_below_threshold_is_never_consulted(self):
        """xAI cannot rescue a setup the mathematics rejected."""
        client = FakeClient(result=approve())
        gate = XAIValidationGate(client=client)
        for count in range(MIN_CONFLUENCES_FOR_XAI):
            decision = gate.gate(context(), count)
            assert decision.consulted is False
            assert decision.may_proceed is False
        assert client.calls == 0, (
            "the model was consulted on a below-threshold setup"
        )

    def test_approval_at_threshold_passes(self):
        gate = XAIValidationGate(client=FakeClient(result=approve()))
        decision = gate.gate(context(), MIN_CONFLUENCES_FOR_XAI)
        assert decision.consulted is True
        assert decision.may_proceed is True

    def test_model_rejection_kills_the_setup(self):
        gate = XAIValidationGate(client=FakeClient(result=reject()))
        decision = gate.gate(context(), 4)
        assert decision.may_proceed is False
        assert decision.model_validate is False

    def test_negative_adjustment_counts_as_a_rejection(self):
        """A model signalling reduced confidence has declined to vouch."""
        gate = XAIValidationGate(client=FakeClient(result=reduce_conf()))
        decision = gate.gate(context(), 5)
        assert decision.may_proceed is False

    def test_provider_failure_fails_closed(self):
        """A gate that cannot reach the model is not a passing gate."""
        gate = XAIValidationGate(
            client=FakeClient(raises=ProviderTimeout("slow")))
        decision = gate.gate(context(), 5)
        assert decision.may_proceed is False
        assert decision.provider_ok is False

    def test_unconfigured_provider_is_never_a_pass(self):
        gate = XAIValidationGate(client=FakeClient(configured=False))
        decision = gate.gate(context(), 5)
        assert decision.may_proceed is False
        assert decision.consulted is False

    def test_gate_cannot_elevate_a_below_threshold_setup(self):
        """The asymmetry, stated as a test.

        A model returning APPROVE must not turn a 2-of-5 into a trade.
        """
        gate = XAIValidationGate(client=FakeClient(result=approve()))
        decision = gate.gate(context(), 2)
        assert decision.may_proceed is False
        assert "below" in decision.reason

    def test_full_confluence_still_requires_the_model(self):
        """5 of 5 does not bypass the gate."""
        gate = XAIValidationGate(client=FakeClient(result=reject()))
        decision = gate.gate(context(), 5)
        assert decision.may_proceed is False


class TestGateLogging:

    def test_rejection_logs_the_specified_message(self, tmp_path):
        from engine.journal import Journal
        journal = Journal(tmp_path / "g.db")
        try:
            gate = XAIValidationGate(client=FakeClient(result=reject()),
                                     journal=journal)
            gate.gate(context(), 4)
            messages = [e["message"] for e in journal.recent_events(50)]
            assert any(REJECTION_LOG in m for m in messages), (
                f"expected {REJECTION_LOG!r} in {messages}"
            )
        finally:
            journal.close()

    def test_provider_failure_logs_a_distinct_message(self, tmp_path):
        from engine.journal import Journal
        journal = Journal(tmp_path / "g2.db")
        try:
            gate = XAIValidationGate(
                client=FakeClient(raises=ProviderTimeout("slow")),
                journal=journal)
            gate.gate(context(), 4)
            messages = [e["message"] for e in journal.recent_events(50)]
            assert any(PROVIDER_FAILURE_LOG in m for m in messages)
            assert not any(REJECTION_LOG in m for m in messages), (
                "a provider outage must not be logged as a model rejection; "
                "one is a decision and the other is a broken system"
            )
        finally:
            journal.close()


class TestSpatialContextPayload:

    def test_payload_carries_exactly_the_six_fields(self):
        payload = context().to_snapshot().to_payload()
        assert set(payload) == {"symbol", "timeframe", "price",
                                "indicators", "context"}
        assert payload["symbol"] == "EURUSD"
        assert payload["timeframe"] == "M15"

    def test_every_context_value_is_numeric(self):
        """The hallucination check bounds numbers, so nothing may be prose."""
        payload = context().to_snapshot().to_payload()
        assert isinstance(payload["price"], float)
        for group in ("indicators", "context"):
            for value in payload[group].values():
                assert isinstance(value, (int, float))

    def test_describe_is_readable_for_the_journal(self):
        text = context().describe()
        assert "EURUSD" in text
        assert "sweep=BUY" in text


# ─────────────────────────────────────────────────────────────────────────────
# 3. Lot sizing
# ─────────────────────────────────────────────────────────────────────────────

class TestLotSizing:

    def test_lots_match_the_documented_formula(self):
        spec = make_spec(tick_value_loss=1.0, tick_size=1e-05,
                         volume_min=0.01, volume_step=0.01)
        sizer = LotSizer(spec)
        # 100 pips = 0.01000 price units = 1000 ticks
        # risk per lot = 1000 * 1.0 = 1000; 10 / 1000 = 0.01
        result = sizer.size(entry=1.10000, stop=1.09000, risk_budget=10.0)
        assert result.ok is True
        assert result.raw_lots == pytest.approx(0.01, rel=1e-6)
        assert result.lots == pytest.approx(0.01)

    def test_tighter_stop_allows_more_size(self):
        spec = make_spec()
        sizer = LotSizer(spec)
        tight = sizer.size(1.10000, 1.09900, 10.0)
        wide = sizer.size(1.10000, 1.09000, 10.0)
        assert tight.raw_lots > wide.raw_lots

    def test_below_minimum_fails_closed(self):
        """A 300-pip stop cannot be carried by a 10 euro budget at 0.01."""
        spec = make_spec(tick_value_loss=1.0, tick_size=1e-05, volume_min=0.01)
        sizer = LotSizer(spec)
        result = sizer.size(entry=1.10000, stop=1.07000, risk_budget=10.0)
        assert result.ok is False
        assert result.lots == 0.0, "no size may be produced on a fail"
        assert LOTS_BELOW_MINIMUM_LOG in result.log_message
        assert LOTS_BELOW_MINIMUM_LOG in result.reason

    def test_sub_minimum_is_never_rounded_up(self):
        """Rounding 0.004 up to 0.01 would quadruple the risk.

        250 pips at tick_value 1.0 is 2500 of risk per lot, so a 10 euro budget
        buys 0.004 lots. Rounding that up to the 0.01 minimum would risk 25,
        two and a half times the cap.
        """
        spec = make_spec(tick_value_loss=1.0, tick_size=1e-05,
                         volume_min=0.01, volume_step=0.01)
        sizer = LotSizer(spec)
        result = sizer.size(entry=1.10000, stop=1.07500, risk_budget=10.0)
        assert result.raw_lots < 0.01, (
            f"fixture no longer produces a sub-minimum size: {result.raw_lots}"
        )
        assert result.ok is False
        assert result.lots == 0.0

    def test_a_size_exactly_on_the_minimum_is_accepted(self):
        """10 / (1000 ticks x 1.0) is mathematically 0.01.

        It computes as 0.009999999999999992 in binary floating point, so a
        naive comparison against the 0.01 minimum rejects an order the rule
        permits. The epsilon in LotSizer exists for exactly this.
        """
        spec = make_spec(tick_value_loss=1.0, tick_size=1e-05,
                         volume_min=0.01, volume_step=0.01)
        result = LotSizer(spec).size(entry=1.10000, stop=1.09000,
                                     risk_budget=10.0)
        assert result.ok is True, (
            f"an order sized exactly to the minimum was rejected: "
            f"raw_lots={result.raw_lots!r} {result.reason}"
        )
        assert result.lots == pytest.approx(0.01)
        assert result.risk_amount <= 10.0 + 1e-6

    def test_step_is_floored_never_rounded_up(self):
        spec = make_spec(volume_step=0.1, volume_min=0.1)
        sizer = LotSizer(spec)
        result = sizer.size(entry=1.10000, stop=1.09000, risk_budget=10.0)
        if result.ok:
            assert result.lots <= 0.1 + 1e-12

    def test_zero_stop_distance_is_refused(self):
        sizer = LotSizer(make_spec())
        result = sizer.size(entry=1.10000, stop=1.10000, risk_budget=10.0)
        assert result.ok is False

    def test_unusable_tick_spec_is_refused(self):
        sizer = LotSizer(make_spec(tick_value_loss=0.0))
        result = sizer.size(entry=1.10000, stop=1.09000, risk_budget=10.0)
        assert result.ok is False
        assert "tick" in result.reason.lower()

    def test_reported_risk_never_exceeds_the_budget(self):
        spec = make_spec()
        sizer = LotSizer(spec)
        for stop in (1.09900, 1.09500, 1.09000, 1.08000):
            result = sizer.size(entry=1.10000, stop=stop, risk_budget=10.0)
            if result.ok:
                assert result.risk_amount <= 10.0 + 1e-6, (
                    f"stop {stop} produced risk {result.risk_amount}, above "
                    f"the 10.00 budget"
                )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Live invalidation guard
# ─────────────────────────────────────────────────────────────────────────────

class TestLiveGuard:

    def test_clear_structure_passes(self):
        guard = LiveGuard(invalidation_check=lambda s: False)
        ok, reason = guard.verify("EURUSD")
        assert ok is True
        assert "clear" in reason

    def test_breached_structure_blocks(self):
        guard = LiveGuard(invalidation_check=lambda s: True)
        ok, reason = guard.verify("EURUSD")
        assert ok is False
        assert "swing anchor" in reason

    def test_a_raising_check_blocks_rather_than_passes(self):
        """An unreadable guard must not assume the structure held."""
        def boom(symbol):
            raise RuntimeError("price feed down")

        guard = LiveGuard(invalidation_check=boom)
        ok, reason = guard.verify("EURUSD")
        assert ok is False
        assert "RuntimeError" in reason

    def test_absent_check_allows(self):
        """No guard configured means no opinion, not a block."""
        guard = LiveGuard()
        ok, _ = guard.verify("EURUSD")
        assert ok is True


class TestExecutorBlocksOnInvalidation:
    """The guard runs inside submit(), not as a suggestion."""

    def _executor(self, breached: bool):
        ex = PaperExecutor(journal=None,
                           tick_provider=lambda s: type(
                               "T", (), {"bid": 1.10000, "ask": 1.10002})())
        ex.live_guard = LiveGuard(
            invalidation_check=lambda s: breached)
        return ex

    def _approval(self):
        return RiskApproval(approved=True, symbol="EURUSD")

    def test_a_breached_structure_prevents_the_fill(self):
        ex = self._executor(breached=True)
        request = ex.prepare_order("EURUSD", "BUY", 0.01, 1.09500)
        with pytest.raises(ExecutionRejected) as excinfo:
            ex.submit(request, self._approval())
        assert "swing anchor" in str(excinfo.value)

    def test_a_clear_structure_fills(self):
        ex = self._executor(breached=False)
        request = ex.prepare_order("EURUSD", "BUY", 0.01, 1.09500)
        result = ex.submit(request, self._approval())
        assert result.ok is True

    def test_invalidation_is_checked_before_the_fill(self):
        """Ordering: the guard must run before a position exists."""
        ex = self._executor(breached=True)
        request = ex.prepare_order("EURUSD", "BUY", 0.01, 1.09500)
        with pytest.raises(ExecutionRejected):
            ex.submit(request, self._approval())
        assert ex.positions() == [], (
            "a position was created despite the invalidation check"
        )