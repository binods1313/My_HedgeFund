"""Per-asset-class volume cap tests.

Tests that a size above the per-symbol cap (volume_min x 10) is rejected,
never clipped, for each of the key asset classes: forex, JPY-crosses,
XAUUSD, XAGUSD.

The cap is computed at runtime from the broker's volume_min:
    cap = volume_min * 10

This mirrors the documented rule and the default in config.yaml
(``volume_cap_default_multiplier: 10``).
"""
from __future__ import annotations

from engine.sizing import LotSizer
from ui.risk_calc import SymbolSpec


def make_spec(
    *,
    volume_min: float = 0.01,
    volume_max: float = 100.0,
    volume_step: float = 0.01,
    tick_value_loss: float = 1.0,
    tick_size: float = 1e-05,
    contract_size: float = 100_000,
    digits: int = 5,
) -> SymbolSpec:
    return SymbolSpec(
        symbol="TEST",
        category="forex",
        digits=digits,
        point=tick_size,
        tick_size=tick_size,
        tick_value_loss=tick_value_loss,
        contract_size=contract_size,
        volume_min=volume_min,
        volume_max=volume_max,
        volume_step=volume_step,
    )


def compute_cap(volume_min: float, multiplier: int = 10) -> float:
    """Mirror the runtime default: volume_min x multiplier."""
    return volume_min * multiplier


class TestForexVolumeCap:
    """Standard-forex pair: volume_min = 0.01, cap = 0.10."""

    def test_size_above_cap_is_rejected_not_clipped(self):
        spec = make_spec(volume_min=0.01)
        cap = compute_cap(0.01)  # 0.10
        sizer = LotSizer(spec, volume_cap=cap)
        # tick_size=1e-5, tick_value=1.0, distance=0.001 (10 pips)
        # risk_per_lot = 0.001/1e-5 * 1 = 100; 5000/100 = 50 lots > 0.10 cap
        result = sizer.size(entry=1.10000, stop=1.09900, risk_budget=5000.0)
        assert not result.ok
        assert result.lots == 0.0, "must never clip — must return zero"
        assert "VOLUME_ABOVE_CAP" in result.reason
        assert f"{cap:g}" in result.reason

    def test_size_below_cap_is_accepted(self):
        spec = make_spec(volume_min=0.01)
        cap = compute_cap(0.01)  # 0.10
        sizer = LotSizer(spec, volume_cap=cap)
        # Same stop; budget=5.0 → 0.05 lots, below 0.10 cap
        result = sizer.size(entry=1.10000, stop=1.09900, risk_budget=5.0)
        assert result.ok
        assert result.lots <= cap
        assert result.lots > 0


class TestJpyCrossVolumeCap:
    """JPY cross: volume_min = 0.01, cap = 0.10, 3-digit tick."""

    def test_jpy_size_above_cap_is_rejected(self):
        spec = make_spec(volume_min=0.01, tick_size=0.01, digits=3)
        cap = compute_cap(0.01)  # 0.10
        sizer = LotSizer(spec, volume_cap=cap)
        # distance=0.05 (5 pips), risk_per_lot=0.05/0.01*1=5; 50/5=10 lots > 0.10
        result = sizer.size(entry=110.0, stop=109.95, risk_budget=50.0)
        assert not result.ok
        assert result.lots == 0.0
        assert "VOLUME_ABOVE_CAP" in result.reason

    def test_jpy_size_below_cap_is_accepted(self):
        spec = make_spec(volume_min=0.01, tick_size=0.01, digits=3)
        cap = compute_cap(0.01)  # 0.10
        sizer = LotSizer(spec, volume_cap=cap)
        # budget=0.25 → 0.05 lots, below 0.10 cap
        result = sizer.size(entry=110.0, stop=109.95, risk_budget=0.25)
        assert result.ok
        assert result.lots <= cap


class TestXauUsdVolumeCap:
    """XAUUSD: volume_min = 0.01, cap = 0.10."""

    def test_xau_size_above_cap_is_rejected(self):
        spec = make_spec(volume_min=0.01, tick_size=0.01, digits=2)
        cap = compute_cap(0.01)  # 0.10
        sizer = LotSizer(spec, volume_cap=cap)
        # distance=0.03 (3 cents), risk_per_lot=0.03/0.01*1=3; 30/3=10 > 0.10
        result = sizer.size(entry=2000.0, stop=1999.97, risk_budget=30.0)
        assert not result.ok
        assert result.lots == 0.0
        assert "VOLUME_ABOVE_CAP" in result.reason

    def test_xau_size_below_cap_is_accepted(self):
        spec = make_spec(volume_min=0.01, tick_size=0.01, digits=2)
        cap = compute_cap(0.01)  # 0.10
        sizer = LotSizer(spec, volume_cap=cap)
        # budget=1.5 → 0.5 lots... wait that's > cap. Use budget=0.2 → 0.067
        result = sizer.size(entry=2000.0, stop=1999.97, risk_budget=0.3)
        assert result.ok
        assert result.lots <= cap


class TestXagUsdVolumeCap:
    """XAGUSD: volume_min = 0.5, cap = 5.0."""

    def test_xag_size_above_cap_is_rejected(self):
        spec = make_spec(volume_min=0.5, tick_size=0.01, digits=2)
        cap = compute_cap(0.5)  # 5.0
        sizer = LotSizer(spec, volume_cap=cap)
        # distance=1.0, risk_per_lot=1.0/0.01*1=100; 6000/100=60 > 5.0
        result = sizer.size(entry=23.0, stop=22.0, risk_budget=6000.0)
        assert not result.ok
        assert result.lots == 0.0
        assert "VOLUME_ABOVE_CAP" in result.reason

    def test_xag_size_below_cap_is_accepted(self):
        spec = make_spec(volume_min=0.5, tick_size=0.01, digits=2)
        cap = compute_cap(0.5)  # 5.0
        sizer = LotSizer(spec, volume_cap=cap)
        # budget=100 → 1.0 lots, below 5.0
        result = sizer.size(entry=23.0, stop=22.0, risk_budget=100.0)
        assert result.ok
        assert result.lots <= cap


class TestVolumeCapValidation:
    """Config integrity: cap below volume_min must fail loudly."""

    def test_cap_below_volume_min_is_rejected(self):
        spec = make_spec(volume_min=0.05)
        # Explicit cap of 0.01 is below volume_min 0.05 — config error
        sizer = LotSizer(spec, volume_cap=0.01)
        result = sizer.size(entry=1.10000, stop=1.09900, risk_budget=10.0)
        assert not result.ok
        assert "VOLUME_CAP_BELOW_MINIMUM" in result.reason

    def test_no_explicit_cap_allows_pass_through(self):
        """LotSizer with volume_cap=None performs no cap check.

        The runtime caller (scan_cycle) computes volume_min * multiplier
        and always passes a value. This test confirms None disables the check.
        """
        spec = make_spec(volume_min=0.01)
        sizer = LotSizer(spec, volume_cap=None)
        # budget=500 → 5.0 lots, would exceed a 0.10 cap — but no cap set
        result = sizer.size(entry=1.10000, stop=1.09900, risk_budget=500.0)
        assert result.ok
        assert result.lots > 0

    def test_zero_cap_disables_check(self):
        spec = make_spec(volume_min=0.01)
        sizer = LotSizer(spec, volume_cap=0)
        result = sizer.size(entry=1.10000, stop=1.09900, risk_budget=500.0)
        assert result.ok
