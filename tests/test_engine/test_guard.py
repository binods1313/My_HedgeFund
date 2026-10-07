"""The pre-dispatch structural guard, tested without MT5.

`engine.guard` is pure by construction -- it takes callables rather than a data
source -- so these run against hand-built legs and stub providers. Every
assertion is about one contract: what the guard does when it *can* see the
market, and what it does when it cannot.

The fail-closed half is the half that matters. A guard that silently passes
when the feed is down is worse than no guard, because it reports coverage it
does not have.

Run: python -m pytest tests/test_engine/test_guard.py -v
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from engine import guard as guard_module
from engine.guard import make_live_guard
from engine.indicators.structure import SwingLeg

#: Any non-empty object will do: `_is_empty` probes `len()` only, and the
#: `leg=` path never touches the frame at all.
BARS = [1, 2, 3]

#: A bullish anchor: invalidated when price falls back through its origin.
BULLISH = SwingLeg(start_index=10, end_index=20, start_price=1.0900,
                   end_price=1.1000, direction="BULLISH", invalidation=1.0900)
#: A bearish anchor: invalidated when price rises back through its origin.
BEARISH = SwingLeg(start_index=10, end_index=20, start_price=1.1000,
                   end_price=1.0900, direction="BEARISH", invalidation=1.1000)


def _guard(*, side: str = "BUY", bars=BARS, price=1.0950, leg=BULLISH):
    return make_live_guard(
        side=side, leg=leg,
        bars_provider=lambda symbol: bars,
        price_provider=lambda symbol: price,
    )


def _unchecked_guard(*, side: str = "BUY", bars=BARS, price=1.0950):
    """A guard with no injected leg, so `_derive_leg` is exercised."""
    return make_live_guard(
        side=side, leg=None,
        bars_provider=lambda symbol: bars,
        price_provider=lambda symbol: price,
    )


# -- the structure is broken --------------------------------------------

def test_a_bullish_anchor_broken_to_the_downside_is_refused() -> None:
    ok, reason = _guard(price=1.0800).verify("EURUSD")

    assert ok is False
    assert "100% swing anchor" in reason


def test_a_bullish_anchor_still_intact_is_allowed() -> None:
    ok, reason = _guard(price=1.0950).verify("EURUSD")

    assert ok is True
    assert reason == "invalidation clear"


def test_a_bearish_anchor_broken_to_the_upside_is_refused() -> None:
    ok, reason = _guard(side="SELL", leg=BEARISH, price=1.1100).verify("EURUSD")

    assert ok is False
    assert "100% swing anchor" in reason


def test_a_bearish_anchor_still_intact_is_allowed() -> None:
    ok, _ = _guard(side="SELL", leg=BEARISH, price=1.0950).verify("EURUSD")

    assert ok is True


# -- the structure cannot be checked ------------------------------------

def test_missing_candles_fail_closed() -> None:
    ok, reason = _guard(bars=None).verify("EURUSD")

    assert ok is False
    assert "RuntimeError" in reason


def test_an_empty_frame_fails_closed() -> None:
    """The feed going quiet must read as 'unverified', not as 'all clear'.

    This is the case a truthiness test on a DataFrame would both raise on and
    get wrong, which is why `_is_empty` treats an empty frame as absent.
    """
    ok, reason = _guard(bars=[]).verify("EURUSD")

    assert ok is False
    assert "RuntimeError" in reason


def test_an_unreadable_price_fails_closed() -> None:
    ok, reason = _guard(price=None).verify("EURUSD")

    assert ok is False
    assert "RuntimeError" in reason


def test_a_provider_that_raises_fails_closed() -> None:
    """A candle source blowing up is indistinguishable from having no candles."""
    def exploding(symbol):
        raise RuntimeError("feed down")

    guard = make_live_guard(side="BUY", leg=BULLISH,
                            bars_provider=exploding,
                            price_provider=lambda symbol: 1.0950)

    ok, reason = guard.verify("EURUSD")

    assert ok is False
    assert "RuntimeError" in reason


# -- derivations that find nothing ---------------------------------------

def test_no_direction_matched_sweep_is_a_pass_not_a_block(monkeypatch) -> None:
    """`None` means 'nothing to invalidate', not 'unverifiable'.

    The structural rules are strict, so this is the common case rather than
    evidence that something broke. Blocking here would refuse nearly every
    manual order -- the guard would be rejecting what it could not evaluate
    instead of what it found broken, which is a different and much weaker rule.
    """
    monkeypatch.setattr(guard_module, "daily_extremes", lambda *a, **k: None)
    monkeypatch.setattr(guard_module, "detect_sweeps", lambda *a, **k: [])

    ok, _ = _unchecked_guard(price=1.0800).verify("EURUSD")

    assert ok is True


def test_a_sweep_with_no_qualifying_leg_is_a_pass_not_a_block(monkeypatch) -> None:
    """A matched sweep that never clears the 3x ATR bar is still a pass."""
    monkeypatch.setattr(guard_module, "daily_extremes", lambda *a, **k: None)
    monkeypatch.setattr(
        guard_module, "detect_sweeps",
        lambda *a, **k: [SimpleNamespace(direction="BUY")])
    monkeypatch.setattr(guard_module, "select_structural_leg", lambda *a, **k: None)

    ok, _ = _unchecked_guard(price=1.0800).verify("EURUSD")

    assert ok is True


def test_a_sweep_for_the_other_side_is_not_used(monkeypatch) -> None:
    """A SELL sweep must not anchor a BUY intent."""
    monkeypatch.setattr(guard_module, "daily_extremes", lambda *a, **k: None)
    monkeypatch.setattr(
        guard_module, "detect_sweeps",
        lambda *a, **k: [SimpleNamespace(direction="SELL")])
    consulted = []

    def spy_leg(bars, *, sweep=None, **kwargs):
        consulted.append(sweep.direction)
        return BULLISH

    monkeypatch.setattr(guard_module, "select_structural_leg", spy_leg)

    ok, _ = _unchecked_guard(side="BUY").verify("EURUSD")

    assert ok is True
    assert consulted == [], (
        "a SELL sweep was handed to select_structural_leg for a BUY intent"
    )


def test_only_the_matching_sweep_signs_the_leg(monkeypatch) -> None:
    """With both directions present, the intent's own side anchors it."""
    monkeypatch.setattr(guard_module, "daily_extremes", lambda *a, **k: None)
    monkeypatch.setattr(
        guard_module, "detect_sweeps",
        lambda *a, **k: [SimpleNamespace(direction="SELL"),
                         SimpleNamespace(direction="BUY")])
    consulted = []

    def spy_leg(bars, *, sweep=None, **kwargs):
        consulted.append(sweep.direction)
        return BULLISH

    monkeypatch.setattr(guard_module, "select_structural_leg", spy_leg)

    _unchecked_guard(side="BUY").verify("EURUSD")

    assert consulted == ["BUY"]


# -- a supplied leg is authoritative -------------------------------------

def test_the_supplied_leg_is_used_verbatim(monkeypatch) -> None:
    """The AI path hands in the anchor its intent was built on.

    Re-deriving from a fresh snapshot would test a *different* structure and
    defeat the point of passing one in, so a supplied leg must short-circuit
    the derivation entirely.
    """
    def must_not_derive(*args, **kwargs):
        raise AssertionError("the supplied leg must be used verbatim")

    monkeypatch.setattr(guard_module, "detect_sweeps", must_not_derive)

    ok, reason = _guard(leg=BULLISH, price=1.0800).verify("EURUSD")

    assert ok is False
    assert "100% swing anchor" in reason


def test_the_supplied_leg_still_requires_a_price() -> None:
    """Having an anchor does not excuse the guard from reading the market."""
    ok, reason = _guard(leg=BULLISH, price=None).verify("EURUSD")

    assert ok is False
    assert "RuntimeError" in reason


def test_a_leg_without_an_invalidation_level_never_blocks() -> None:
    """`is_invalidated` returns False for a falsy anchor; the guard passes it on.

    Documented rather than desired: a leg with no recorded invalidation price
    cannot be breached, so it cannot block. The check that such a leg is never
    produced belongs to the strategy, not to the guard.
    """
    no_level = SwingLeg(start_index=1, end_index=2, start_price=1.0900,
                        end_price=1.1000, direction="BULLISH", invalidation=0.0)

    ok, _ = _guard(leg=no_level, price=1.0000).verify("EURUSD")

    assert ok is True


@pytest.mark.parametrize("bad_side", ["", None, "buyy"])
def test_an_unrecognised_side_matches_no_sweep(bad_side, monkeypatch) -> None:
    """Sanitising the side here is deliberate: the caller validates it.

    An unrecognised side matches no sweep, so there is nothing to invalidate.
    The order itself is refused upstream by `submit_order`'s own direction
    check, which is where that decision belongs.
    """
    monkeypatch.setattr(guard_module, "daily_extremes", lambda *a, **k: None)
    monkeypatch.setattr(
        guard_module, "detect_sweeps",
        lambda *a, **k: [SimpleNamespace(direction="BUY"),
                         SimpleNamespace(direction="SELL")])

    guard = make_live_guard(side=bad_side, leg=None,
                            bars_provider=lambda symbol: BARS,
                            price_provider=lambda symbol: 1.0950)

    ok, _ = guard.verify("EURUSD")

    assert ok is True
