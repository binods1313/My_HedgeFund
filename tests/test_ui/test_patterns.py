"""Hand-built candles for the eleven chart patterns."""

from ui.patterns import (
    FILTER_CHOICES,
    MARKER_LABELS,
    PATTERN_GROUPS,
    PATTERN_NAMES,
    SENTIMENT,
    allows_pattern,
    describe_trigger,
    detect_patterns,
    marker_label,
    pattern_group,
)


def test_eleven_names_are_listed():
    assert len(PATTERN_NAMES) == 11


def test_hammer_after_a_decline():
    opens = [1.20, 1.18, 1.16, 1.14, 1.100]
    highs = [1.21, 1.19, 1.17, 1.15, 1.106]
    lows = [1.17, 1.15, 1.13, 1.11, 1.080]
    closes = [1.18, 1.16, 1.14, 1.12, 1.104]
    hits = detect_patterns(opens, highs, lows, closes, upto=len(closes))
    hammer = [hit for hit in hits if hit.name == "Hammer"]
    assert len(hammer) == 1
    assert hammer[0].index == 4
    assert hammer[0].bias == "bullish"
    assert hammer[0].sl == 1.080
    assert hammer[0].tp == 1.104 + 3.0 * (1.104 - 1.080)


def test_shooting_star_after_a_rise():
    # Body is more than 10% of the range, so this is not a gravestone doji.
    opens = [1.00, 1.02, 1.04, 1.06, 1.100]
    highs = [1.03, 1.05, 1.07, 1.09, 1.250]
    lows = [0.99, 1.01, 1.03, 1.05, 1.092]
    closes = [1.02, 1.04, 1.06, 1.08, 1.140]
    hits = detect_patterns(opens, highs, lows, closes, upto=len(closes))
    star = [hit for hit in hits if hit.index == 4]
    assert len(star) == 1
    assert star[0].name == "Shooting Star"
    assert star[0].bias == "bearish"
    assert star[0].sl == 1.250
    assert star[0].tp == 1.140 - 3.0 * (1.250 - 1.140)


def test_hanging_man_inverted_hammer_and_bearish_pair():
    hanging = detect_patterns(
        [1.00, 1.02, 1.04, 1.06, 1.100],
        [1.03, 1.05, 1.07, 1.09, 1.106],
        [0.99, 1.01, 1.03, 1.05, 1.080],
        [1.02, 1.04, 1.06, 1.08, 1.104],
        upto=5,
    )
    assert hanging[-1].name == "Hanging Man"
    assert hanging[-1].bias == "bearish"

    inverted = detect_patterns(
        [1.22, 1.18, 1.15, 1.10, 1.100],
        [1.24, 1.20, 1.17, 1.13, 1.250],
        [1.16, 1.14, 1.12, 1.08, 1.092],
        [1.18, 1.16, 1.14, 1.12, 1.140],
        upto=5,
    )
    assert inverted[-1].name == "Inverted Hammer"
    assert inverted[-1].bias == "bullish"

    bear_maru = detect_patterns([2.0], [2.01], [0.99], [1.0], upto=1)
    assert bear_maru[0].name == "Bearish Marubozu"
    assert bear_maru[0].bias == "bearish"

    bear_engulf = detect_patterns(
        [1.00, 1.12],
        [1.12, 1.13],
        [0.98, 0.97],
        [1.10, 0.99],
        upto=2,
    )
    assert bear_engulf[-1].name == "Bearish Engulfing"
    assert bear_engulf[-1].bias == "bearish"


def test_doji_is_neutral_and_has_no_stops():
    hits = detect_patterns(
        [1.0], [1.5], [0.5], [1.01], upto=1,
    )
    assert len(hits) == 1
    assert hits[0].name == "Doji"
    assert hits[0].bias == "neutral"
    assert hits[0].sl is None and hits[0].tp is None


def test_dragonfly_beats_plain_doji():
    hits = detect_patterns([1.0], [1.02], [0.2], [1.01], upto=1)
    assert hits[0].name == "Dragonfly Doji"
    assert hits[0].bias == "bullish"


def test_marubozu_and_engulfing():
    maru = detect_patterns([1.0], [2.01], [0.99], [2.0], upto=1)
    assert maru[0].name == "Bullish Marubozu"
    engulf = detect_patterns(
        [1.10, 0.99],
        [1.12, 1.13],
        [0.98, 0.97],
        [1.00, 1.11],
        upto=2,
    )
    assert engulf[-1].name == "Bullish Engulfing"
    assert engulf[-1].bias == "bullish"


def test_forming_bar_is_ignored_and_does_not_change_earlier_hits():
    opens = [1.20, 1.18, 1.16, 1.14, 1.100, 9.0]
    highs = [1.21, 1.19, 1.17, 1.15, 1.106, 9.5]
    lows = [1.17, 1.15, 1.13, 1.11, 1.080, 8.5]
    closes = [1.18, 1.16, 1.14, 1.12, 1.104, 9.2]
    closed = detect_patterns(opens, highs, lows, closes, upto=len(closes) - 1)
    closes[-1] = 0.1
    highs[-1] = 12.0
    again = detect_patterns(opens, highs, lows, closes, upto=len(closes) - 1)
    assert [hit.name for hit in closed] == [hit.name for hit in again]
    assert all(hit.index < len(closes) - 1 for hit in again)


def test_zero_range_bar_is_skipped():
    assert detect_patterns([1.0], [1.0], [1.0], [1.0], upto=1) == []


def test_every_name_has_a_short_mark_group_and_sentiment():
    for name in PATTERN_NAMES:
        label = marker_label(name)
        assert label == MARKER_LABELS[name]
        assert " " not in label
        assert len(label) <= 6
        assert name not in label
        assert pattern_group(name) == PATTERN_GROUPS[name]
        assert "Agent" not in SENTIMENT[name]
        assert "%" not in SENTIMENT[name]


def test_filter_splits_reversal_indecision_and_momentum():
    assert set(FILTER_CHOICES) >= {"All patterns", "Reversals only", "Indecision only", "Momentum only", "Hide all"}
    assert allows_pattern("Hammer", "Reversals only")
    assert not allows_pattern("Doji", "Reversals only")
    assert allows_pattern("Doji", "Indecision only")
    assert allows_pattern("Bullish Marubozu", "Momentum only")
    assert not allows_pattern("Hammer", "Hide all")
    assert allows_pattern("Shooting Star", "All patterns")


def test_trigger_text_quotes_the_threshold_and_not_an_agent_score():
    text = describe_trigger([1.0], [1.10], [0.90], [1.01], 0, "Doji")
    assert "10%" in text
    assert "1.00000" in text
    assert "Agent" not in text
    maru = describe_trigger([1.0], [2.01], [0.99], [2.0], 0, "Bullish Marubozu")
    assert "90%" in maru
    star = describe_trigger(
        [1.00, 1.02, 1.04, 1.06, 1.100],
        [1.03, 1.05, 1.07, 1.09, 1.250],
        [0.99, 1.01, 1.03, 1.05, 1.092],
        [1.02, 1.04, 1.06, 1.08, 1.140],
        4,
        "Shooting Star",
    )
    assert "2×" in star or "2x" in star
    assert "rising" in star
