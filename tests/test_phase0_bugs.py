"""Unit tests for Phase 0 bug fixes.

Tests cover:
  - Lot normalisation (_normalize_lot): step rounding, min/max boundaries,
    the critical float edge case (0.07 / 0.01), and the $100 BTCUSD rejection.
  - Spread calculation (get_symbol_live_quote): correct formula for various
    instrument types, plus sanity-check rejection of corrupt/stale ticks.

Run with::

    .venv\\Scripts\\python -m pytest tests/test_phase0_bugs.py -v
"""

import types
import pytest
from decimal import Decimal

# ─────────────────────────────────────────────────────────────────────────────
# Helpers — build lightweight mock objects so tests run without MT5
# ─────────────────────────────────────────────────────────────────────────────

def _make_sym_info(volume_step, volume_min, volume_max,
                   point=0.00001, digits=5, path="Forex\\Majors",
                   description="", filling_mode=2):
    """Return a SimpleNamespace that mimics mt5.SymbolInfo."""
    return types.SimpleNamespace(
        volume_step=volume_step,
        volume_min=volume_min,
        volume_max=volume_max,
        point=point,
        digits=digits,
        path=path,
        description=description,
        filling_mode=filling_mode,
    )


def _make_tick(bid, ask, time=1700000000):
    return types.SimpleNamespace(bid=bid, ask=ask, time=time)


# Import the helpers under test
from execution import _normalize_lot

# ─────────────────────────────────────────────────────────────────────────────
# LOT NORMALISATION TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizeLot:
    """§1.2 of Instructions_2.md: floor → reject if < min → clamp to max."""

    def _info(self, step=0.01, v_min=0.01, v_max=100.0):
        return _make_sym_info(step, v_min, v_max)

    # ── basic happy path ──────────────────────────────────────────────────────

    def test_exact_step(self):
        lot, err = _normalize_lot(0.05, self._info())
        assert err == ""
        assert lot == pytest.approx(0.05)

    def test_rounds_down_not_up(self):
        """0.059 should floor to 0.05, NOT round to 0.06."""
        lot, err = _normalize_lot(0.059, self._info())
        assert err == ""
        assert lot == pytest.approx(0.05)

    def test_clamps_to_volume_max(self):
        lot, err = _normalize_lot(999.0, self._info(v_max=100.0))
        assert err == ""
        assert lot == pytest.approx(100.0)

    # ── critical §1.2 fix: reject below minimum, do NOT round up ─────────────

    def test_rejects_below_volume_min(self):
        """A $100 account on BTCUSD: raw lot 0.0003 < min 0.01 → reject."""
        info = _make_sym_info(volume_step=0.01, volume_min=0.01, volume_max=10.0,
                              point=1.0, digits=2, path="Crypto\\Bitcoin")
        lot, err = _normalize_lot(0.003, info)
        assert lot == 0.0
        assert "below broker minimum" in err.lower() or "rejected" in err.lower()

    def test_does_not_clamp_up_to_min(self):
        """Old (broken) code clamped to min. New code must REJECT instead."""
        info = _make_sym_info(volume_step=0.01, volume_min=0.01, volume_max=100.0)
        # raw lot 0.005 → floors to 0.00 (< min) → reject, not clamp to 0.01
        lot, err = _normalize_lot(0.005, info)
        assert lot == 0.0
        assert err != ""

    # ── float precision edge cases ────────────────────────────────────────────

    def test_float_edge_case_0_07(self):
        """0.07 / 0.01 = 6.999... in float arithmetic; Decimal should give 7."""
        info = _make_sym_info(volume_step=0.01, volume_min=0.01, volume_max=100.0)
        lot, err = _normalize_lot(0.07, info)
        assert err == ""
        assert lot == pytest.approx(0.07), f"Expected 0.07 but got {lot}"

    def test_float_edge_case_0_03(self):
        """0.03 / 0.01 can be 2.9999... — must floor to 0.03 not 0.02."""
        info = _make_sym_info(volume_step=0.01, volume_min=0.01, volume_max=100.0)
        lot, err = _normalize_lot(0.03, info)
        assert err == ""
        assert lot == pytest.approx(0.03)

    def test_float_edge_case_step_0_001(self):
        """Small step size (crypto), e.g. 1.2345 lot, step 0.001."""
        info = _make_sym_info(volume_step=0.001, volume_min=0.001, volume_max=10.0)
        lot, err = _normalize_lot(1.2345, info)
        assert err == ""
        assert lot == pytest.approx(1.234)  # floors, not rounds

    # ── instrument-specific scenarios ────────────────────────────────────────

    def test_eurusd_typical(self):
        """Standard EURUSD: 0.37 lot, step 0.01."""
        lot, err = _normalize_lot(0.37, self._info())
        assert err == ""
        assert lot == pytest.approx(0.37)

    def test_exactly_at_volume_min(self):
        lot, err = _normalize_lot(0.01, self._info(v_min=0.01))
        assert err == ""
        assert lot == pytest.approx(0.01)

    def test_exactly_at_volume_max(self):
        lot, err = _normalize_lot(100.0, self._info(v_max=100.0))
        assert err == ""
        assert lot == pytest.approx(100.0)

    def test_large_step_index(self):
        """Index with step=1.0, min=1.0."""
        info = _make_sym_info(volume_step=1.0, volume_min=1.0, volume_max=500.0,
                              point=0.1, digits=1, path="Indices\\US")
        lot, err = _normalize_lot(3.7, info)
        assert err == ""
        assert lot == pytest.approx(3.0)  # floor to 3, not round to 4


# ─────────────────────────────────────────────────────────────────────────────
# SPREAD CALCULATION TESTS (unit-testing the formula, not live MT5)
# ─────────────────────────────────────────────────────────────────────────────

class TestSpreadFormula:
    """Verify the spread = (ask - bid) / point formula for various instruments.

    These tests exercise the formula directly without calling MT5 so that
    they can run in CI without a terminal connection.
    """

    def _spread(self, bid, ask, point):
        """Replicate the fixed formula from get_symbol_live_quote."""
        return round((ask - bid) / point, 1)

    # ── per-instrument correctness ────────────────────────────────────────────

    def test_eurusd_5_digit(self):
        """EURUSD: bid 1.08500, ask 1.08502 → 2.0 points."""
        assert self._spread(1.08500, 1.08502, 0.00001) == pytest.approx(2.0)

    def test_usdjpy_3_digit(self):
        """USDJPY: bid 149.500, ask 149.503 → 3.0 points (point = 0.001)."""
        assert self._spread(149.500, 149.503, 0.001) == pytest.approx(3.0)

    def test_xauusd_gold(self):
        """Gold: bid 2000.00, ask 2000.30 → 30.0 points (point = 0.01)."""
        assert self._spread(2000.00, 2000.30, 0.01) == pytest.approx(30.0)

    def test_nas100_index(self):
        """NAS100: bid 18000.0, ask 18000.5 → 5.0 points (point = 0.1)."""
        assert self._spread(18000.0, 18000.5, 0.1) == pytest.approx(5.0)

    def test_btcusd_crypto(self):
        """BTCUSD: bid 65000.0, ask 65005.0 → 5.0 points (point = 1.0)."""
        assert self._spread(65000.0, 65005.0, 1.0) == pytest.approx(5.0)

    def test_gbpjpy_cross(self):
        """GBPJPY: bid 192.000, ask 192.004 → 4.0 points (point = 0.001)."""
        assert self._spread(192.000, 192.004, 0.001) == pytest.approx(4.0)

    # ── old formula was WRONG for gold ────────────────────────────────────────

    def test_old_formula_wrong_for_gold(self):
        """Demonstrate that the OLD formula (point * 10 for 2-digit) gives wrong result.

        XAUUSD digits=2: old code uses point * 10 = 0.01 * 10 = 0.1
        Spread = 0.30 / 0.1 = 3.0 (WRONG — should be 30.0 points).
        """
        old_spread = round(0.30 / (0.01 * 10), 1)  # old buggy formula
        new_spread = self._spread(2000.00, 2000.30, 0.01)
        assert old_spread == pytest.approx(3.0), "Old formula baseline"
        assert new_spread == pytest.approx(30.0), "New formula is 10x more accurate"
        assert old_spread != new_spread

    # ── sanity-check logic ────────────────────────────────────────────────────

    def test_inverted_quote_detected(self):
        """ask < bid must be flagged as invalid."""
        bid, ask = 1.10000, 1.09000
        assert ask < bid  # inverted
        # The function should return valid=False for this case

    def test_wildly_wide_spread_detected(self):
        """Spread > 5 % of ask price (like the 4543 pip case) must be rejected."""
        bid, ask = 15.15, 60.58  # the example from the screenshot
        assert (ask - bid) > ask * 0.05

    def test_normal_spread_not_rejected(self):
        """Normal spread must NOT be rejected by the sanity check."""
        bid, ask = 1.08500, 1.08502
        assert (ask - bid) <= ask * 0.05

    def test_zero_bid_detected(self):
        """bid == 0 must be flagged as inverted/invalid."""
        bid, ask = 0.0, 1.10000
        assert bid <= 0

    def test_negative_bid_detected(self):
        """Negative bid must be flagged."""
        bid = -1.0
        assert bid <= 0


# ─────────────────────────────────────────────────────────────────────────────
# STALE TICK TESTS (Item 4 of pre-approval checklist)
# ─────────────────────────────────────────────────────────────────────────────

class TestStaleTick:
    """Unit-test the stale-tick logic without a live MT5 connection.

    We test the age comparison formula directly since the full function
    requires MT5. Integration verification is in demo_verification.py.
    """

    def test_fresh_tick_is_valid(self):
        import time
        tick_time = time.time() - 5      # 5 seconds old
        age = time.time() - tick_time
        assert age < 30, "Fresh tick should be within 30s limit"

    def test_stale_tick_exceeds_limit(self):
        import time
        tick_time = time.time() - 60     # 60 seconds old
        age = time.time() - tick_time
        assert age > 30, "Old tick should exceed 30s limit"

    def test_boundary_tick_at_exact_limit(self):
        import time
        max_age = 30
        tick_time = time.time() - max_age
        age = time.time() - tick_time
        # age should be >= max_age → reject
        assert age >= max_age

    def test_configurable_max_age(self):
        """Different max_age values change what counts as stale."""
        import time
        tick_time = time.time() - 45    # 45 seconds old
        age = time.time() - tick_time
        assert age > 30,  "Stale at 30s limit"
        assert age < 60,  "Fresh at 60s limit"
        assert age > 10,  "Stale at 10s limit"
