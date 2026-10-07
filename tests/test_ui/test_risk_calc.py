import math

import pytest

from ui.risk_calc import (
    SymbolSpec,
    normalize_lots,
    portfolio_open_risk,
    trade_risk,
    validate_pre_submit,
    verify_margin,
)


@pytest.fixture
def eurusd_spec() -> SymbolSpec:
    return SymbolSpec(
        symbol="EURUSD",
        category="forex",
        digits=5,
        point=0.00001,
        tick_size=0.00001,
        tick_value_loss=1.0,
        contract_size=100000.0,
        volume_min=0.01,
        volume_max=10.0,
        volume_step=0.01,
        stops_level_points=0,
    )


def test_trade_risk_matches_worked_examples(eurusd_spec: SymbolSpec) -> None:
    risk_amount, risk_pct = trade_risk(eurusd_spec, 1.0, 30.0, 1000.0)
    assert round(risk_amount, 6) == 300.0
    assert round(risk_pct or 0.0, 6) == 30.0
    assert trade_risk(eurusd_spec, 0.08, 30.0)[0] == pytest.approx(24.0)


def test_normalize_lots_rounds_down_to_step_and_bounds(eurusd_spec: SymbolSpec) -> None:
    assert normalize_lots(0.138, eurusd_spec) == 0.13
    assert normalize_lots(0.01, eurusd_spec) == 0.01
    assert normalize_lots(0.001, eurusd_spec) == 0.01
    assert normalize_lots(50.0, eurusd_spec) == 10.0


def test_normalize_lots_rejects_non_finite_and_invalid_step(eurusd_spec: SymbolSpec) -> None:
    with pytest.raises(ValueError):
        normalize_lots(math.nan, eurusd_spec)
    invalid_spec = SymbolSpec(**{**eurusd_spec.__dict__, "volume_step": 0.0})
    with pytest.raises(ValueError):
        normalize_lots(1.0, invalid_spec)


def test_non_forex_uses_points_and_jpy_uses_forex_pips() -> None:
    jpy = SymbolSpec("USDJPY", "forex", 3, 0.001, 0.001, 6.5, 100000, 0.01, 10, 0.01)
    index = SymbolSpec("US500", "indices", 1, 0.1, 0.1, 1.0, 1, 0.1, 100, 0.1)
    assert trade_risk(jpy, 1.0, 30.0)[0] == pytest.approx(1950.0)
    assert trade_risk(index, 1.0, 30.0)[0] == pytest.approx(30.0)


def test_portfolio_risk_marks_missing_stop_unbounded(eurusd_spec: SymbolSpec) -> None:
    result = portfolio_open_risk(
        [
            {"symbol": "EURUSD", "volume": 1.0, "price_open": 1.1, "sl": 1.097},
            {"symbol": "EURUSD", "volume": 0.1, "price_open": 1.1, "sl": 0},
        ],
        {"EURUSD": eurusd_spec},
        equity=1000,
    )
    assert result.unbounded
    assert math.isinf(result.amount)
    assert result.percent is None
    assert result.position_risks[0] == pytest.approx(300.0)


def test_margin_verification_guards_unknown_and_zero_denominator() -> None:
    unknown = verify_margin(free_margin=1000, equity=1000, margin_used=0, required=None)
    assert not unknown.verified
    sufficient = verify_margin(free_margin=1000, equity=1000, margin_used=0, required=100)
    assert sufficient.sufficient
    assert sufficient.projected_free_margin == pytest.approx(900)
    assert sufficient.projected_margin_level == pytest.approx(1000)
    insufficient = verify_margin(free_margin=100, equity=1000, margin_used=0, required=101)
    assert not insufficient.sufficient


def test_validator_returns_structured_failures(eurusd_spec: SymbolSpec) -> None:
    report = validate_pre_submit(
        spec=eurusd_spec,
        symbol="EURUSD",
        lots=0,
        stop_units=0,
        equity=1000,
        required=None,
        free_margin=1000,
        margin_used=0,
        engine_state="paused",
        whitelist=["GBPUSD"],
        order_token="repeat",
        used_tokens=["repeat"],
    )
    codes = {failure.code for failure in report.failures}
    assert {
        "ENGINE_NOT_READY", "SYMBOL_NOT_WHITELISTED", "LOTS_INVALID",
        "SL_REQUIRED", "MARGIN_INSUFFICIENT", "DUPLICATE_SUBMIT",
    } <= codes
    assert report.failure_matrix[0].keys() == {"code", "message"}


def test_validator_enforces_risk_spread_sl_distance_and_slippage(eurusd_spec: SymbolSpec) -> None:
    spec = SymbolSpec(**{**eurusd_spec.__dict__, "stops_level_points": 100})
    report = validate_pre_submit(
        spec=spec,
        symbol="EURUSD",
        lots=1.0,
        stop_units=1.0,
        equity=1000,
        required=100,
        free_margin=1000,
        margin_used=100,
        max_risk_per_trade_pct=0.5,
        max_total_risk_pct=5.0,
        open_risk=100,
        spread_points=10,
        max_spread_points=5,
        slippage=3,
        max_slippage=2,
    )
    codes = {failure.code for failure in report.failures}
    assert {"SL_TOO_CLOSE", "RISK_PER_TRADE_EXCEEDED", "TOTAL_RISK_EXCEEDED", "SPREAD_TOO_WIDE", "SLIPPAGE_INVALID"} <= codes


def test_validator_reports_malformed_numbers_without_raising(eurusd_spec: SymbolSpec) -> None:
    report = validate_pre_submit(
        spec=eurusd_spec,
        symbol="EURUSD",
        lots="not-a-number",
        stop_units="bad-sl",
        equity=None,
        required="unknown",
        free_margin=1000,
        margin_used=0,
        heartbeat_age_s=float("nan"),
        slippage=float("nan"),
    )
    codes = {failure.code for failure in report.failures}
    assert {"ENGINE_NOT_READY", "LOTS_INVALID", "SL_REQUIRED", "EQUITY_INVALID", "MARGIN_INSUFFICIENT", "SLIPPAGE_INVALID"} <= codes


def test_unbounded_open_risk_lists_tickets_without_changing_the_fail(eurusd_spec: SymbolSpec) -> None:
    report = validate_pre_submit(
        spec=eurusd_spec,
        symbol="EURUSD",
        lots=0.01,
        stop_units=30,
        equity=10000,
        required=10,
        free_margin=10000,
        margin_used=0,
        max_total_risk_pct=5.0,
        open_risk=float("inf"),
        unprotected_tickets=[101, 202],
    )
    messages = [failure.message for failure in report.failures if failure.code == "TOTAL_RISK_EXCEEDED"]
    assert messages == [
        "Open positions without a stop loss are counted as unlimited risk. Tickets: #101, #202"
    ]


def test_missing_ticket_list_still_names_unlimited_risk(eurusd_spec: SymbolSpec) -> None:
    report = validate_pre_submit(
        spec=eurusd_spec,
        symbol="EURUSD",
        lots=0.01,
        stop_units=30,
        equity=10000,
        required=10,
        free_margin=10000,
        margin_used=0,
        max_total_risk_pct=5.0,
        open_risk=float("inf"),
    )
    messages = [failure.message for failure in report.failures if failure.code == "TOTAL_RISK_EXCEEDED"]
    assert messages == [
        "Open positions without a stop loss are counted as unlimited risk. Tickets: —"
    ]


def test_finite_total_risk_message_stays_the_cap_sentence(eurusd_spec: SymbolSpec) -> None:
    report = validate_pre_submit(
        spec=eurusd_spec,
        symbol="EURUSD",
        lots=1.0,
        stop_units=30,
        equity=1000,
        required=100,
        free_margin=1000,
        margin_used=0,
        max_total_risk_pct=5.0,
        open_risk=100,
        unprotected_tickets=[7],
    )
    messages = [failure.message for failure in report.failures if failure.code == "TOTAL_RISK_EXCEEDED"]
    assert messages == ["Open portfolio risk plus this trade exceeds the configured cap"]


def test_validator_can_keep_engine_heartbeat_informational(eurusd_spec: SymbolSpec) -> None:
    report = validate_pre_submit(
        spec=eurusd_spec,
        symbol="EURUSD",
        lots=0.1,
        stop_units=30,
        equity=1000,
        required=100,
        free_margin=1000,
        margin_used=0,
        engine_state="offline",
        connected=False,
        heartbeat_age_s=None,
        require_engine_ready=False,
        whitelist=["EURUSD"],
        market_open=True,
        tick_fresh=True,
    )
    assert "ENGINE_NOT_READY" not in {failure.code for failure in report.failures}
