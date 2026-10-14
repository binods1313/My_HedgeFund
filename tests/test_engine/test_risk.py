"""Risk Engine tests.

The gate is fail-closed, so most of these assert that something BLOCKS. That is
the point: a risk engine that approves when it cannot evaluate a limit converts
an unknown into a false all-clear.

Each limit is tested on its own, then in combination, then for the halt
semantics that distinguish an automatic clear from an operator-only one.

Run: python -m pytest tests/test_engine/test_risk.py -v
"""
import pytest

from engine.journal import Journal
from engine.risk import (
    AccountState,
    OrderIntent,
    RiskConfigError,
    RiskEngine,
    RiskLimits,
)

# A healthy account: 100,000 equity, nothing open, peak equals current.
HEALTHY = dict(
    equity=100_000.0,
    start_of_day_equity=100_000.0,
    peak_equity=100_000.0,
    open_trade_count=0,
    total_open_risk=0.0,
    unprotected_positions=0,
)


def intent(volume=0.10, entry=1.10000, sl=1.09500, symbol="EURUSD",
           tick_value=1.0, tick_size=0.00001, spread=None, max_spread=None):
    return OrderIntent(symbol=symbol, side="BUY", volume=volume,
                       entry_price=entry, stop_loss=sl,
                       tick_value=tick_value, tick_size=tick_size,
                       spread_points=spread, max_spread_points=max_spread)


def account(**overrides):
    data = dict(HEALTHY)
    data.update(overrides)
    return AccountState(**data)


def engine(journal=None, **limits):
    return RiskEngine(journal=journal, limits=RiskLimits(**limits) if limits
                      else RiskLimits())


# ─────────────────────────────────────────────────────────────────────────────
# Risk computation
# ─────────────────────────────────────────────────────────────────────────────

class TestRiskAmount:

    def test_risk_is_entry_to_stop_distance(self):
        # 50 pips at 0.01 lots with tick_value 1.0 per 0.00001:
        # 500 ticks * 1.0 * 0.1 lot = 50
        assert intent().risk_amount == pytest.approx(50.0)

    def test_risk_scales_with_volume(self):
        one = intent(volume=0.10).risk_amount
        two = intent(volume=0.20).risk_amount
        assert two == pytest.approx(2 * one)

    def test_risk_is_absolute_for_a_short(self):
        """A short's stop sits above entry; the distance must still count."""
        long_side = intent(entry=1.10000, sl=1.09500)
        short_side = OrderIntent(symbol="EURUSD", side="SELL", volume=0.10,
                                 entry_price=1.10000, stop_loss=1.10500,
                                 tick_value=1.0, tick_size=0.00001)
        assert short_side.risk_amount == pytest.approx(long_side.risk_amount)

    def test_wider_stop_means_more_risk(self):
        tight = intent(sl=1.09900).risk_amount
        wide = intent(sl=1.09000).risk_amount
        assert wide > tight

    def test_no_stop_has_no_defined_risk(self):
        no_stop = intent(sl=0.0)
        assert no_stop.has_stop is False


class TestStopLossIsMandatory:
    """The gate must REFUSE an unstoppable order, not merely flag it.

    Both positions the reconcile dropped on 2026-10-12 (USDCAD ticket
    10726505976, GBPUSD ticket 10745483547) were open with sl=0.0 and tp=0.0,
    and each was carrying a loss. `has_stop` being False was already true; what
    had never been asserted is that `evaluate()` actually blocks on it.
    """

    def test_the_risk_gate_refuses_an_order_with_no_stop(self):
        decision = engine().evaluate(intent(sl=0.0), account())
        assert decision.approved is False, (
            "an order without a stop loss must be refused by the risk gate"
        )

    def test_the_refusal_names_the_missing_stop(self):
        decision = engine().evaluate(intent(sl=0.0), account())
        assert "order has no stop loss, so its worst case is undefined" \
            in decision.violations

    def test_a_short_without_a_stop_is_refused_too(self):
        """A short can hide behind "above" pricing; the distance still counts."""
        bare = OrderIntent(symbol="EURUSD", side="SELL", volume=0.10,
                           entry_price=1.10000, stop_loss=0.0,
                           tick_value=1.0, tick_size=0.00001)
        assert engine().evaluate(bare, account()).approved is False

    def test_the_missing_stop_short_circuits_the_other_checks(self):
        """The refusal must not depend on any other limit being breached.

        The account here is healthy on every axis, so if the order is still
        blocked then the missing stop alone did it -- and the engine never
        reached the per-trade, exposure or drawdown arithmetic.
        """
        decision = engine().evaluate(intent(volume=0.10, sl=0.0), account())
        assert decision.approved is False
        assert decision.violations == [
            "order has no stop loss, so its worst case is undefined"
        ], (
            "a short-circuit means this is the only violation reported; "
            "further checks would be evaluated against an undefined worst case"
        )

    def test_supplying_a_stop_and_nothing_else_approves(self):
        """The stop is the only thing standing between this order and approval.

        Proves the test above is refusing for the right reason: identical
        order, identical account, one stop loss added, and it passes.
        """
        assert engine().evaluate(intent(), account()).approved is True

    def test_zero_tick_value_is_refused_at_construction(self):
        """Without a tick spec the risk cannot be computed, so the order is
        malformed rather than merely risky."""
        with pytest.raises(RiskConfigError) as excinfo:
            intent(tick_value=0.0)
        assert "tick_value" in str(excinfo.value)


class TestTickSpecConversion:
    """The live probe showed tick_value is the value of ONE tick_size move.

    Dividing by tick_size instead of scaling by it reported EURUSD as 88,861.24
    per point, where the real figure is 0.8886. At that magnitude every order
    would breach the per-trade cap by five orders of magnitude, so the wrong
    direction here silently blocks all trading.
    """

    # Values measured from MetaQuotes-Demo via probe_tick_specs.py.
    EURUSD = dict(tick_value=0.8886124316879194, tick_size=1e-05,
                  point=1e-05, digits=5)
    XAUUSD = dict(tick_value=0.1, tick_size=0.01, point=0.01, digits=2)

    def test_five_digit_forex_point_conversion(self):
        spec = self.EURUSD
        per_point = spec["tick_value"] * (spec["point"] / spec["tick_size"])
        assert per_point == pytest.approx(0.8886124, abs=1e-6)
        assert per_point < 1.0, "a point of EURUSD cannot be worth 100,000 EUR"

    def test_gold_point_conversion(self):
        spec = self.XAUUSD
        per_point = spec["tick_value"] * (spec["point"] / spec["tick_size"])
        assert per_point == pytest.approx(0.1, abs=1e-9)

    def test_a_pip_is_worth_the_market_standard(self):
        """EURUSD should be ~8.89 account currency per pip per lot.

        The account is EUR-denominated, so the figure is EUR 8.886 rather than
        the usual USD 8.89. This pins the conversion against a number every
        trader would recognise, which is a stronger check than a self-consistent
        one: a formula that is internally consistent but off by a factor of
        ten would otherwise pass unnoticed.
        """
        spec = self.EURUSD
        pip_size = spec["point"] * 10          # 5-digit quote: 10 points
        per_pip = pip_size / spec["tick_size"] * spec["tick_value"]
        assert per_pip == pytest.approx(8.886, abs=0.01), (
            f"EURUSD pip value came out at {per_pip}; the market standard is "
            f"about 8.89 per pip per lot"
        )

    def test_ten_pip_stop_on_a_tenth_of_a_lot(self):
        """0.10 lots with a 10 pip stop risks about EUR 8.89."""
        spec = self.EURUSD
        pip_size = spec["point"] * 10
        distance = pip_size * 10
        risk = (distance / spec["tick_size"]
                * spec["tick_value"] * 0.10)
        assert risk == pytest.approx(8.886, abs=0.01), (
            f"implied risk EUR {risk:.2f} for a 10 pip stop on 0.10 lots"
        )

    def test_wider_stop_scales_linearly(self):
        """Doubling the stop distance doubles the risk."""
        spec = self.EURUSD
        base = OrderIntent.from_symbol_spec(
            symbol="EURUSD", side="BUY", volume=0.10, entry_price=1.10000,
            stop_loss=1.09900,
            tick_value=spec["tick_value"], tick_size=spec["tick_size"])
        wide = OrderIntent.from_symbol_spec(
            symbol="EURUSD", side="BUY", volume=0.10, entry_price=1.10000,
            stop_loss=1.09800,
            tick_value=spec["tick_value"], tick_size=spec["tick_size"])
        assert wide.risk_amount == pytest.approx(2 * base.risk_amount, rel=1e-6)

    def test_wrong_conversion_is_detectably_absurd(self):
        """Pins why the conversion matters rather than trusting it."""
        spec = self.EURUSD
        correct = spec["tick_value"] * (spec["point"] / spec["tick_size"])
        wrong = spec["tick_value"] / spec["tick_size"]
        assert wrong / correct > 10_000, (
            "the incorrect conversion must be orders of magnitude off, so any "
            "order sized with it is obviously broken"
        )

    def test_from_symbol_spec_accepts_live_values(self):
        spec = self.EURUSD
        intent = OrderIntent.from_symbol_spec(
            symbol="EURUSD", side="BUY", volume=0.10, entry_price=1.10000,
            stop_loss=1.09900,
            tick_value=spec["tick_value"], tick_size=spec["tick_size"])
        # 10 pips on 0.10 lots, from live broker values.
        assert intent.risk_amount == pytest.approx(8.886, abs=0.01)

    @pytest.mark.parametrize("tick_value,tick_size", [
        (0.0, 1e-05), (0.888, 0.0), (None, 1e-05), (0.888, None),
    ])
    def test_from_symbol_spec_refuses_a_missing_tick_field(self, tick_value,
                                                          tick_size):
        """An unmeasurable symbol cannot be sized, so it must be refused."""
        with pytest.raises(RiskConfigError) as excinfo:
            OrderIntent.from_symbol_spec(
                symbol="X", side="BUY", volume=0.1, entry_price=1.1,
                stop_loss=1.095, tick_value=tick_value, tick_size=tick_size)
        assert "tick" in str(excinfo.value).lower()


# ─────────────────────────────────────────────────────────────────────────────
# Limits
# ─────────────────────────────────────────────────────────────────────────────

class TestPerTradeRisk:
    """Base arithmetic: 0.10 lots at 50 pips with tick_value 1.0 is 50 account
    currency, so risk scales as `volume * 500`.

    On a 100,000 account the 1% cap is 1,000 of risk, reached at 2.0 lots.
    """

    def test_base_risk_amount(self):
        assert intent(volume=0.10).risk_amount == pytest.approx(50.0)

    def test_within_limit_is_approved(self):
        decision = engine().evaluate(intent(volume=0.10), account())
        assert decision.approved is True
        assert decision.violations == []

    def test_risk_pct_is_reported(self):
        # 1.0 lot = 500 risk = 0.5% of 100,000.
        decision = engine().evaluate(intent(volume=1.0), account())
        assert decision.risk_pct == pytest.approx(0.5, abs=1e-6)

    def test_above_limit_is_blocked(self):
        # 1% of 100k = 1,000 of risk, so 2.0 lots is exactly 1% and 2.2 lots
        # (1,100) is 1.1% and must be refused.
        decision = engine().evaluate(intent(volume=2.2), account())
        assert decision.approved is False
        assert any("per-trade" in v for v in decision.violations)

    def test_exactly_at_limit_is_allowed(self):
        """The limit is inclusive: 2.0 lots is exactly 1% and must pass.

        Float noise makes this arrive as 1.0000000000000002%, which is why the
        engine compares with a relative tolerance.
        """
        decision = engine(risk_per_trade_pct=1.0).evaluate(
            intent(volume=2.0), account())
        assert decision.approved is True, f"violations: {decision.violations}"
        assert decision.risk_pct == pytest.approx(1.0, abs=1e-6)

    def test_a_hair_over_the_limit_is_still_refused(self):
        """Tolerance must not swallow a genuine breach."""
        decision = engine(risk_per_trade_pct=1.0).evaluate(
            intent(volume=2.002), account())
        assert decision.approved is False, (
            "2.002 lots is 1.001%, a real breach that tolerance must not excuse"
        )


class TestOpenTradeCount:

    def test_at_limit_blocks(self):
        decision = engine().evaluate(
            intent(), account(open_trade_count=5))
        assert decision.approved is False
        assert any("open trades" in v for v in decision.violations)

    def test_below_limit_allows(self):
        decision = engine().evaluate(
            intent(), account(open_trade_count=4))
        assert decision.approved is True

    def test_custom_limit_is_honoured(self):
        engine_2 = engine(max_open_trades=2)
        assert engine_2.evaluate(
            intent(), account(open_trade_count=2)).approved is False
        assert engine_2.evaluate(
            intent(), account(open_trade_count=1)).approved is True


class TestTotalExposure:

    def test_within_limit_allows(self):
        decision = engine().evaluate(
            intent(volume=0.10), account(total_open_risk=500.0))
        assert decision.approved is True

    def test_projected_over_limit_blocks(self):
        # 10% of 100k = 10,000. Existing 9,980 plus 50 new = 10,030 > 10,000.
        decision = engine().evaluate(
            intent(volume=0.10), account(total_open_risk=9_980.0))
        assert decision.approved is False
        assert any("exposure" in v for v in decision.violations)

    def test_projected_exactly_at_limit_is_allowed(self):
        """Inclusive boundary: projected exposure equal to the cap passes."""
        decision = engine().evaluate(
            intent(volume=0.10), account(total_open_risk=9_950.0))
        assert decision.approved is True, (
            f"9,950 + 50 is exactly the 10,000 cap and must pass; "
            f"violations: {decision.violations}"
        )

    def test_unknown_total_risk_blocks(self):
        """A portfolio limit reading zero because the data is missing is not
        a limit."""
        decision = engine().evaluate(intent(), account(total_open_risk=None))
        assert decision.approved is False
        assert any("total open risk is unknown" in v for v in decision.violations)


class TestDailyLoss:

    def test_within_limit_allows(self):
        # 2% down = 2000 loss, limit 3%
        decision = engine().evaluate(
            intent(), account(equity=98_000.0, peak_equity=100_000.0))
        assert decision.approved is True

    def test_beyond_limit_blocks(self):
        # 4% down = 4000 loss, limit 3%
        decision = engine().evaluate(
            intent(), account(equity=96_000.0, peak_equity=100_000.0))
        assert decision.approved is False
        assert any("daily loss" in v for v in decision.violations)

    def test_profit_never_counts_as_a_loss(self):
        decision = engine().evaluate(
            intent(), account(equity=101_000.0, peak_equity=101_000.0))
        assert decision.approved is True

    def test_unknown_start_of_day_blocks(self):
        decision = engine().evaluate(
            intent(), account(start_of_day_equity=None))
        assert decision.approved is False
        assert any("start-of-day" in v for v in decision.violations)


class TestDrawdown:

    def test_within_limit_allows(self):
        # 9% below peak, but the day started at today's equity, so there is no
        # daily loss. Both baselines must be set consistently: start-of-day at
        # 91,000 makes the drawdown 9% without inventing a same-day loss.
        decision = engine().evaluate(
            intent(), account(equity=91_000.0, peak_equity=100_000.0,
                              start_of_day_equity=91_000.0))
        assert decision.approved is True, f"violations: {decision.violations}"

    def test_beyond_limit_blocks(self):
        # 11% below peak, with no same-day loss.
        decision = engine().evaluate(
            intent(), account(equity=89_000.0, peak_equity=100_000.0,
                              start_of_day_equity=89_000.0))
        assert decision.approved is False
        assert any("drawdown" in v for v in decision.violations)

    def test_drawdown_and_daily_loss_are_independent(self):
        """A position open from yesterday can breach drawdown without a daily
        loss. Both limits must be evaluated separately."""
        decision = engine().evaluate(
            intent(), account(equity=91_000.0, peak_equity=100_000.0,
                              start_of_day_equity=100_000.0))
        assert decision.approved is False, (
            "9% down from the start of the day is a 9% daily loss and must block"
        )
        assert any("daily loss" in v for v in decision.violations)

    def test_peak_is_not_lower_than_equity(self):
        """Equity above the recorded peak must not produce a negative drawdown."""
        decision = engine().evaluate(
            intent(), account(equity=105_000.0, peak_equity=100_000.0,
                              start_of_day_equity=100_000.0))
        assert decision.approved is True

    def test_unknown_peak_blocks(self):
        decision = engine().evaluate(intent(), account(peak_equity=None))
        assert decision.approved is False
        assert any("peak equity" in v for v in decision.violations)


class TestUnprotectedPositions:

    def test_any_unprotected_position_blocks(self):
        """Unquantified risk cannot be sized around."""
        decision = engine().evaluate(
            intent(), account(unprotected_positions=1))
        assert decision.approved is False
        assert any("no stop loss" in v for v in decision.violations)

    def test_zero_unprotected_allows(self):
        decision = engine().evaluate(
            intent(), account(unprotected_positions=0))
        assert decision.approved is True


class TestSpreadLimit:

    def test_within_limit_allows(self):
        decision = engine().evaluate(intent(spread=20.0), account())
        assert decision.approved is True

    def test_above_limit_blocks(self):
        decision = engine().evaluate(intent(spread=45.0), account())
        assert decision.approved is False
        assert any("spread" in v for v in decision.violations)

    def test_per_symbol_override_wins(self):
        decision = engine().evaluate(
            intent(spread=45.0, max_spread=60.0), account())
        assert decision.approved is True, "a wider per-symbol cap must apply"

    def test_per_symbol_override_can_be_tighter(self):
        decision = engine().evaluate(
            intent(spread=15.0, max_spread=10.0), account())
        assert decision.approved is False, (
            "a per-symbol cap tighter than the default must apply"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Fail-closed behaviour
# ─────────────────────────────────────────────────────────────────────────────

class TestFailClosed:

    def test_missing_equity_blocks(self):
        decision = engine().evaluate(intent(), account(equity=None))
        assert decision.approved is False
        assert any("equity is unknown" in v for v in decision.violations)

    def test_zero_equity_blocks(self):
        decision = engine().evaluate(intent(), account(equity=0.0))
        assert decision.approved is False

    def test_order_without_stop_blocks(self):
        decision = engine().evaluate(intent(sl=0.0), account())
        assert decision.approved is False
        assert any("no stop loss" in v for v in decision.violations)

    def test_every_unknown_reports_its_own_reason(self):
        """Each missing input must be named, so the operator knows what to fix.

        The exposure check short-circuits when equity is unknown, since
        projected exposure is a percentage. Supplying equity here exercises the
        remaining unknowns.
        """
        decision = engine().evaluate(
            intent(),
            account(equity=100_000.0, total_open_risk=None,
                    start_of_day_equity=None, peak_equity=None))
        blob = " ".join(decision.violations)
        for expected in ("total open risk is unknown",
                         "start-of-day equity is unknown",
                         "peak equity is unknown"):
            assert expected in blob, f"missing reason: {expected}\ngot: {blob}"

    def test_missing_equity_names_equity_and_its_consequences(self):
        """A missing equity blocks and explains the knock-on effects."""
        decision = engine().evaluate(
            intent(),
            account(equity=None, total_open_risk=None,
                    start_of_day_equity=None, peak_equity=None))
        blob = " ".join(decision.violations)
        assert "equity is unknown" in blob
        assert "per-trade" in blob
        assert "daily loss" in blob
        assert "drawdown" in blob


# ─────────────────────────────────────────────────────────────────────────────
# Halts
# ─────────────────────────────────────────────────────────────────────────────

class TestHalts:

    def test_halt_blocks_everything(self):
        risk = engine()
        risk.halt("drawdown", "drawdown limit breached")
        decision = risk.evaluate(intent(), account())
        assert decision.approved is False
        assert any("halted" in v for v in decision.violations)

    def test_daily_loss_halt_clears_on_a_new_day(self):
        risk = engine()
        risk.halt("daily_loss", "daily loss limit breached")
        assert risk.halted is True
        risk.clear_daily_loss_halt()
        assert risk.halted is False, (
            "a daily-loss halt must clear for the next trading day"
        )
        assert risk.evaluate(intent(), account()).approved is True

    def test_drawdown_halt_does_not_clear_automatically(self):
        """A hard stop that resumes itself is not a control."""
        risk = engine()
        risk.halt("drawdown", "drawdown limit breached")
        risk.clear_daily_loss_halt()
        assert risk.halted is True, (
            "drawdown must not clear on a daily-loss reset path"
        )
        assert risk.evaluate(intent(), account()).approved is False

    def test_drawdown_clears_only_by_operator(self):
        risk = engine()
        risk.halt("drawdown", "drawdown limit breached")
        assert risk.clear_manual_halt("reviewed the ledger") is True
        assert risk.halted is False

    def test_manual_halt_is_not_cleared_by_the_daily_path(self):
        risk = engine()
        risk.halt("manual", "operator paused")
        risk.clear_daily_loss_halt()
        assert risk.halted is True

    def test_clear_manual_refuses_a_daily_loss_halt(self):
        risk = engine()
        risk.halt("daily_loss", "daily loss limit breached")
        assert risk.clear_manual_halt() is False

    def test_clear_manual_on_a_healthy_engine_is_a_noop(self):
        risk = engine()
        assert risk.clear_manual_halt() is False
        assert risk.halted is False

    def test_account_level_halt_also_blocks(self):
        risk = engine()
        decision = risk.evaluate(
            intent(), account(trading_halted=True, halt_reason="broker halt"))
        assert decision.approved is False
        assert any("broker halt" in v for v in decision.violations)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ───────────────────────────────────────────────────────────────────────────────────────────────────

class TestRiskConfigValidation:

    @pytest.mark.parametrize("field,value", [
        ("risk_per_trade_pct", 0.0),
        ("risk_per_trade_pct", 101.0),
        ("daily_loss_limit_pct", -1.0),
        ("max_drawdown_pct", 0.0),
        ("max_total_exposure_pct", 150.0),
    ])
    def test_out_of_range_limits_are_refused(self, field, value):
        with pytest.raises(RiskConfigError):
            RiskLimits(**{field: value})

    def test_zero_open_trades_limit_is_refused(self):
        with pytest.raises(RiskConfigError):
            RiskLimits(max_open_trades=0)

    def test_unknown_key_is_refused(self):
        """A typo must not silently leave the default in place."""
        with pytest.raises(RiskConfigError) as excinfo:
            RiskLimits.from_config({"risk_per_trade_pc": 1.0})
        assert "unknown risk keys" in str(excinfo.value)

    def test_limits_load_from_the_shipped_config(self):
        import yaml
        raw = yaml.safe_load(
            (REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
        limits = RiskLimits.from_config(raw["risk"])
        assert limits.risk_per_trade_pct == 1.0
        assert limits.daily_loss_limit_pct == 3.0
        assert limits.max_drawdown_pct == 10.0
        assert limits.max_open_trades == 5
        assert limits.max_total_exposure_pct == 10.0
        assert limits.default_max_spread_points == 30

    def test_shipped_limits_are_sane(self):
        """Guards against a config edit that would silently widen the perimeter."""
        import yaml
        raw = yaml.safe_load(
            (REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
        limits = RiskLimits.from_config(raw["risk"])
        assert limits.risk_per_trade_pct <= 2.0, "per-trade risk above 2% is loose"
        assert limits.max_total_exposure_pct <= 20.0, "portfolio cap above 20% is loose"
        assert limits.max_drawdown_pct <= 15.0, "a drawdown stop above 15% is loose"
        assert limits.max_open_trades <= 10, "more than 10 concurrent trades is loose"


# ─────────────────────────────────────────────────────────────────────────────
# Journal
# ───────────────────────────────────────────────────────────────────────────────────────────────────

class TestDecisionJournal:

    def test_every_decision_is_recorded(self, tmp_path):
        journal = Journal(tmp_path / "risk.db")
        try:
            risk = engine(journal=journal)
            risk.evaluate(intent(), account())                      # approved
            risk.evaluate(intent(volume=21.0), account())          # blocked (1.05%)
            rows = journal.query("SELECT * FROM risk_decisions ORDER BY id")
            assert len(rows) == 2, (
                "both the approval and the refusal must be journaled"
            )
            assert rows[0]["approved"] == 1
            assert rows[1]["approved"] == 0
            assert rows[1]["reasons_json"], "a refusal must carry its reasons"
        finally:
            journal.close()

    def test_approval_is_also_logged(self, tmp_path):
        journal = Journal(tmp_path / "risk2.db")
        try:
            engine(journal=journal).evaluate(intent(), account())
            messages = [e["message"] for e in journal.recent_events(50, "risk")]
            assert any("approved" in m for m in messages)
        finally:
            journal.close()

    def test_halt_writes_an_error_event(self, tmp_path):
        journal = Journal(tmp_path / "risk3.db")
        try:
            risk = engine(journal=journal)
            risk.halt("drawdown", "limit breached")
            errors = [e for e in journal.recent_events(50)
                      if e["severity"] == "ERROR"]
            assert errors, "a halt must be a high-priority event"
        finally:
            journal.close()

    def test_decision_booleans(self):
        risk = engine()
        assert bool(risk.evaluate(intent(), account())) is True
        assert bool(risk.evaluate(intent(volume=21.0), account())) is False


from pathlib import Path  # noqa: E402  (used by config tests above)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

class TestHasStopNeverRaises:
    """An unusable stop is False, never an exception.

    Every value below reaches the engine in practice: MT5 reports a missing
    stop as ``0.0``; both positions the reconcile dropped carried ``0.0``; the
    order form hands through whatever the operator typed. The fail-closed path
    is the one path that must not throw, because a crash there looks like an
    engine fault rather than a refused order.
    """

    @pytest.mark.parametrize("stop", [
        None, "", "   ", "0", "0.0", 0, 0.0, -1, -0.0001, "abc", "abc",
        float("nan"), float("inf") * 0,
    ], ids=str)
    def test_invalid_stops_return_false(self, stop):
        bare = OrderIntent(symbol="EURUSD", side="BUY", volume=0.10,
                           entry_price=1.10000, stop_loss=stop,
                           tick_value=1.0, tick_size=0.00001)
        assert bare.has_stop is False, (
            "an unusable stop must read as no stop, not raise"
        )

    @pytest.mark.parametrize("stop", [1.09500, "1.095", 0.00001, 1.11111],
                             ids=str)
    def test_valid_stops_return_true(self, stop):
        good = OrderIntent(symbol="EURUSD", side="BUY", volume=0.10,
                           entry_price=1.10000, stop_loss=stop,
                           tick_value=1.0, tick_size=0.00001)
        assert good.has_stop is True

    def test_a_stop_at_the_entry_is_not_a_stop(self):
        """Zero distance is an unbounded order wearing a stop's clothes."""
        at_entry = OrderIntent(symbol="EURUSD", side="BUY", volume=0.10,
                               entry_price=1.10000, stop_loss=1.10000,
                               tick_value=1.0, tick_size=0.00001)
        assert at_entry.has_stop is False

    def test_a_zero_stop_does_not_report_a_distance(self):
        """``abs(1.1 - 0.0)`` is 1.1 -- a risk distance taken to price zero.

        Both positions the reconcile dropped carried ``sl=0.0``. Reporting a
        distance for them would have sized a position against a stop that did
        not exist, which is the precise failure this guards.
        """
        zero = OrderIntent(symbol="EURUSD", side="BUY", volume=0.10,
                           entry_price=1.10000, stop_loss=0.0,
                           tick_value=1.0, tick_size=0.00001)
        assert zero.stop_distance == 0.0

    def test_stop_distance_never_raises_either(self):
        for stop in (None, "", "abc", float("nan")):
            bare = OrderIntent(symbol="EURUSD", side="BUY", volume=0.10,
                               entry_price=1.10000, stop_loss=stop,
                               tick_value=1.0, tick_size=0.00001)
            assert bare.stop_distance == 0.0

    def test_the_risk_gate_still_refuses_an_invalid_stop(self):
        """The gate decision, not just the flag."""
        bare = OrderIntent(symbol="EURUSD", side="BUY", volume=0.10,
                           entry_price=1.10000, stop_loss="0",
                           tick_value=1.0, tick_size=0.00001)
        assert engine().evaluate(bare, account()).approved is False

    def test_a_nan_entry_price_is_also_safe(self):
        """NaN poisons both sides; it must not produce a spurious approval."""
        bare = OrderIntent(symbol="EURUSD", side="BUY", volume=0.10,
                           entry_price=float("nan"), stop_loss=1.095,
                           tick_value=1.0, tick_size=0.00001)
        assert bare.has_stop is False