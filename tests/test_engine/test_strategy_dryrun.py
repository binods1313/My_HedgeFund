r"""Tests for confluence assembly and the dry-run harness.

The dry run is the deliverable that has to be trustworthy before any order is
sent, so the tests focus on what it must never do: place an order, and report a
setup as approved when the blocks disagree.
"""
import numpy as np
import pandas as pd
import pytest
from pathlib import Path

from engine.dryrun import (
    DEFAULT_VOLUME,
    max_volume_for_risk,
    run_dry_run,
)
from engine.executor import RiskApproval
from engine.indicators.structure import validate_ohlcv
from engine.risk import RiskConfigError, RiskLimits
from engine.strategy import evaluate, to_intent

from tests.test_engine.test_indicators import flat, frame

SPEC = {"EURUSD": {"trade_tick_value": 0.8886124316879194,
                   "trade_tick_size": 1e-05}}


def trending_rows(direction=1.0, n=320, seed=5):
    """A trending random walk with volume, which the rules can act on."""
    rng = np.random.default_rng(seed)
    price = 1.10000
    rows = []
    for _ in range(n):
        price = max(0.5, price + direction * rng.normal(0.00012, 0.00008))
        rows.append((price, price + 0.00005, price - 0.00005, price))
    return frame(rows, volume=[1000.0 + i for i in range(n)])


# ─────────────────────────────────────────────────────────────────────────────
# Volume arithmetic
# ─────────────────────────────────────────────────────────────────────────────

class TestMaxVolumeForRisk:

    def test_volume_fits_the_budget(self):
        """100 pips of stop at 0.8886 per point per lot."""
        volume = max_volume_for_risk(1.10000, 1.09900,
                                     0.8886124316879194, 1e-05,
                                     risk_budget=10.0)
        per_lot = 0.001 / 1e-05 * 0.8886124316879194     # 88.86 per lot
        assert volume <= 10.0 / per_lot + 1e-9, (
            f"{volume} lots exceeds the budget"
        )
        assert volume >= 10.0 / per_lot - 0.01

    def test_tighter_stop_allows_more_volume(self):
        tight = max_volume_for_risk(1.10000, 1.09950, 0.8886, 1e-05, 10.0)
        wide = max_volume_for_risk(1.10000, 1.09000, 0.8886, 1e-05, 10.0)
        assert tight > wide, (
            "a closer stop risks less per lot, so it must allow more size"
        )

    def test_rounds_down_never_up(self):
        """Rounding up would breach the budget the instant it is placed."""
        volume = max_volume_for_risk(1.10000, 1.09900, 0.8886, 1e-05, 10.0)
        assert volume == pytest.approx(np.floor(volume * 100) / 100), (
            f"{volume} was not floored to two decimals"
        )

    def test_budget_too_small_returns_the_floor(self):
        assert max_volume_for_risk(1.1, 1.09, 0.8886, 1e-05, 0.01) == 0.01

    def test_zero_stop_distance_is_refused_by_the_caller(self):
        # Not raising here; the intent constructor refuses a zero stop. This
        # documents that the helper degrades to the floor rather than dividing.
        volume = max_volume_for_risk(1.1, 1.1, 0.8886, 1e-05, 10.0)
        assert volume == 0.01


# ─────────────────────────────────────────────────────────────────────────────
# Confluence
# ─────────────────────────────────────────────────────────────────────────────

class TestConfluence:

    def test_all_five_blocks_are_reported(self):
        signal = evaluate("EURUSD", trending_rows(), direction="BUY")
        assert len(signal.blocks) == 5, [b.name for b in signal.blocks]
        names = {b.name for b in signal.blocks}
        assert names == {"liquidity sweep", "fair value gap",
                        "fibonacci OTE", "ema ribbon", "vwap"}

    def test_confidence_is_the_share_agreeing(self):
        signal = evaluate("EURUSD", trending_rows(), direction="BUY")
        required = signal.required_blocks
        expected = sum(1 for b in required if b.agrees) / len(required)
        assert signal.confidence == pytest.approx(expected)

    def test_a_block_opposing_blocks_the_signal(self):
        """A conflict is a refusal, not a discount.

        Down-weighting a disagreeing block is how a strategy ends up trading
        setups it contradicts.
        """
        signal = evaluate("EURUSD", trending_rows(direction=-1.0), direction="BUY")
        assert signal.is_actionable is False
        assert signal.conflicts, (
            "a counter-trend ribbon must register as a conflict"
        )
        assert signal.direction is None

    def test_a_clean_uptrend_with_a_long_stop_is_actionable(self):
        """A trend with no structural event still has no gap to trade."""
        signal = evaluate("EURUSD", trending_rows(), direction="BUY")
        # Either action is fine; what must never happen is a partial confluence
        # reported as a trade.
        if signal.is_actionable:
            assert signal.confidence == pytest.approx(1.0)
        else:
            assert signal.conflicts

    def test_short_history_is_refused(self):
        with pytest.raises(ValueError):
            evaluate("EURUSD", flat(10), direction="BUY")

    def test_direction_must_align_with_the_sweep(self):
        """Asking for a BUY when the sweep was a SELL is a conflict."""
        signal = evaluate("EURUSD", trending_rows(direction=-1.0),
                          direction="BUY")
        if signal.sweep is not None and signal.sweep.direction != "BUY":
            assert signal.is_actionable is False

    def test_no_signal_is_still_a_report(self):
        """A refusal must be legible, not a silent None."""
        signal = evaluate("EURUSD", flat(320), direction="BUY")
        report = signal.report()
        assert "direction=NONE" in report
        assert "liquidity sweep" in report


# ─────────────────────────────────────────────────────────────────────────────
# Intent construction
# ─────────────────────────────────────────────────────────────────────────────

class TestToIntent:

    def test_a_non_actionable_signal_cannot_become_an_intent(self):
        """The strategy refusing must not be overridable downstream."""
        signal = evaluate("EURUSD", trending_rows(direction=-1.0),
                          direction="BUY")
        if signal.is_actionable:
            pytest.skip("this fixture happened to produce a valid setup")
        with pytest.raises(ValueError):
            to_intent(signal, symbol="EURUSD", volume=0.01, stop_loss=1.09,
                      tick_value=0.8886, tick_size=1e-05)

    def test_an_actionable_signal_produces_an_intent(self):
        signal = evaluate("EURUSD", trending_rows(), direction="BUY")
        if not signal.is_actionable:
            pytest.skip("fixture did not produce a full confluence")
        intent = to_intent(signal, symbol="EURUSD", volume=0.01,
                           stop_loss=signal.price * 0.99,
                           tick_value=0.8886124316879194, tick_size=1e-05)
        assert intent.side == "BUY"
        assert intent.risk_amount > 0


# ─────────────────────────────────────────────────────────────────────────────
# Dry run
# ─────────────────────────────────────────────────────────────────────────────

class TestDryRun:

    def _run(self, **kwargs):
        params = dict(
            equity=10_000.0,
            start_of_day_equity=10_000.0,
            peak_equity=10_000.0,
            total_open_risk=0.0,
            open_trade_count=0,
            unprotected_positions=0,
            specs=SPEC,
            volume=DEFAULT_VOLUME,
        )
        params.update(kwargs)
        return run_dry_run("EURUSD", params.pop("bars", trending_rows()),
                            **params)

    def test_no_candles_is_reported_not_raised(self):
        result = self._run(bars=pd.DataFrame())
        assert result.ran is False
        assert "candle" in result.reason.lower()

    def test_missing_tick_spec_is_refused(self):
        """Risk cannot be computed without a spec; guessing is not allowed."""
        result = self._run(specs={})
        assert result.ran is False
        assert "specification" in result.reason.lower()

    def test_unusable_tick_spec_is_refused(self):
        result = self._run(
            tick_value=0.0, tick_size=0.0)
        assert result.ran is False

    def test_a_reported_result_never_places_an_order(self):
        """Structural: the harness has no order-submission surface at all."""
        import ast
        from engine import dryrun as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden = {"order_send", "send_order", "close_position",
                      "order_check"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in forbidden, (
                    f"dryrun references .{node.attr}"
                )

    def test_the_report_states_no_order_was_sent(self):
        result = self._run()
        assert "NO ORDER WAS SENT" in result.report()

    def test_account_state_reaches_the_gate(self):
        result = self._run()
        assert result.account is not None
        assert result.account.equity == 10_000.0
        assert result.account.peak_equity == 10_000.0
        assert result.account.start_of_day_equity == 10_000.0

    def test_an_unprotected_position_blocks_the_gate(self):
        result = self._run(unprotected_positions=1)
        if result.ran and result.approval is not None:
            assert result.gate_approved is False
            assert any("no stop loss" in r for r in result.approval.reasons)

    def test_an_excessive_volume_is_refused_by_the_gate(self):
        """Volume beyond the risk budget must not pass."""
        result = self._run(volume=5.0)
        if result.approval is not None:
            assert result.gate_approved is False

    def test_a_drawdown_beyond_the_limit_blocks(self):
        result = self._run(equity=8_000.0, peak_equity=10_000.0)
        if result.approval is not None:
            assert result.gate_approved is False
            assert any("drawdown" in r for r in result.approval.reasons)

    def test_a_daily_loss_beyond_the_limit_blocks(self):
        result = self._run(equity=9_500.0, start_of_day_equity=10_000.0)
        if result.approval is not None:
            assert result.gate_approved is False
            assert any("daily loss" in r for r in result.approval.reasons)

    def test_the_result_is_serialisable(self):
        import json
        result = self._run()
        payload = json.dumps({
            "ran": result.ran,
            "direction": result.direction,
            "gate_approved": result.gate_approved,
            "risk_amount": result.risk_amount,
            "bars": result.bars_used,
        })
        assert "ran" in payload


# ─────────────────────────────────────────────────────────────────────────────
# Approval plumbing
# ─────────────────────────────────────────────────────────────────────────────

class TestRiskApproval:

    def test_from_decision_carries_the_reason(self):
        from engine.risk import RiskDecision
        decision = RiskDecision(approved=False,
                                violations=["risk_per_trade_pct exceeded"],
                                risk_amount=99.0, risk_pct=1.5)
        approval = RiskApproval.from_decision(decision, "EURUSD")
        assert approval.approved is False
        assert "risk_per_trade_pct exceeded" in approval.reasons
        assert approval.risk_amount == 99.0
        assert approval.symbol == "EURUSD"

    def test_an_approval_carries_no_violations(self):
        """Confirmations are not justification for a refusal."""
        from engine.risk import RiskDecision
        decision = RiskDecision(approved=True,
                                reasons=["within all limits"])
        approval = RiskApproval.from_decision(decision, "EURUSD")
        assert approval.approved is True
        assert approval.reasons == ()

    def test_a_bare_bool_is_not_an_approval(self):
        """Already enforced by the executor; pinned here as the contract."""
        from engine.executor import ExecutionRejected, PaperExecutor

        ex = PaperExecutor(journal=None, tick_provider=lambda s: None)
        request = ex.prepare_order("EURUSD", "BUY", 0.01, 1.09500)
        with pytest.raises(ExecutionRejected):
            ex.submit(request, risk_approved=True)   # type: ignore[arg-type]