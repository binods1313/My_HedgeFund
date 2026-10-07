from ui.alerts import alert_signature, margin_is_low, risk_exceeds_cap, should_show
from ui.equity_curve import clean_equity_points
from ui.order_types import UNSUPPORTED, unsupported_order_message


def test_alert_signature_and_dismiss() -> None:
    signature = alert_signature([20, 10, 20], margin_low=True, risk_high=False)
    assert signature == "sl:10,20|margin"
    assert should_show(signature, None)
    assert not should_show(signature, signature)
    assert should_show("sl:10,20|margin|risk", signature)
    assert not should_show(None, None)
    assert margin_is_low(None) is False
    assert margin_is_low(99.0) is True
    assert risk_exceeds_cap(float("inf"), 1000.0, 10.0) is True
    assert risk_exceeds_cap(50.0, 1000.0, 10.0) is False


def test_unsupported_order_sentence() -> None:
    assert unsupported_order_message("Market") == ""
    assert unsupported_order_message("limit") == UNSUPPORTED
    assert unsupported_order_message("Conditional") == "engine does not support this yet"


def test_equity_cleaner_drops_gaps_and_non_positive() -> None:
    stamps = [0, 10, 20, 5000, 5010]
    values = [10, 0, 12, 30, 31]
    cleaned = clean_equity_points(stamps, values, max_gap_s=1800)
    assert [point[1] for point in cleaned] == [30, 31]
    assert clean_equity_points([0], [float("nan")]) == []
