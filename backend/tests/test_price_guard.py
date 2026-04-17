"""Unit tests for PriceGuard entry filters."""

from filters.price_guard import PriceGuard


def test_short_entry_blocked_when_yes_price_too_low():
    guard = PriceGuard()
    allowed, reason = guard.is_allowed(
        entry_price=17.0,
        direction="short",
        atr_regime="MEDIUM",
        time_remaining_sec=600,
    )
    assert allowed is False
    assert reason is not None
    assert reason.startswith("SHORT_ENTRY_TOO_CHEAP")


def test_short_entry_allowed_above_floor_when_other_bounds_ok():
    guard = PriceGuard()
    allowed, reason = guard.is_allowed(
        entry_price=25.0,
        direction="short",
        atr_regime="MEDIUM",
        time_remaining_sec=600,
    )
    assert allowed is True
    assert reason is None


def test_long_entry_still_uses_existing_price_bounds():
    guard = PriceGuard()
    allowed, reason = guard.is_allowed(
        entry_price=10.0,
        direction="long",
        atr_regime="MEDIUM",
        time_remaining_sec=600,
    )
    assert allowed is False
    assert reason is not None
    assert reason.startswith("YES_PRICE_TOO_LOW")
