"""Unit tests for PriceGuard entry filters."""

from filters.price_guard import PriceGuard


def test_short_entry_blocked_when_yes_price_too_low():
    guard = PriceGuard()
    allowed, reason = guard.is_allowed(
        entry_price=17.0,
        direction="short",
        atr_regime="MEDIUM",
        time_remaining_sec=900,
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
        time_remaining_sec=900,
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


# ── Short-entry expiry-window guard (added 2026-04-28) ────────────────────

def test_short_blocked_inside_expiry_window():
    """Default cutoff is 780s (13 min). At 779s shorts must be rejected."""
    guard = PriceGuard()
    allowed, reason = guard.is_allowed(
        entry_price=30.0,
        direction="short",
        atr_regime="MEDIUM",
        time_remaining_sec=779,
    )
    assert allowed is False
    assert reason is not None
    assert reason.startswith("SHORT_ENTRY_TOO_CLOSE_TO_EXPIRY")


def test_short_allowed_at_or_above_expiry_window():
    """At exactly the threshold (780s) and beyond, shorts proceed."""
    guard = PriceGuard()
    allowed, reason = guard.is_allowed(
        entry_price=30.0,
        direction="short",
        atr_regime="MEDIUM",
        time_remaining_sec=780,
    )
    assert allowed is True
    assert reason is None

    allowed, reason = guard.is_allowed(
        entry_price=30.0,
        direction="short",
        atr_regime="MEDIUM",
        time_remaining_sec=900,
    )
    assert allowed is True
    assert reason is None


def test_long_unaffected_by_short_expiry_guard():
    """The new guard is short-only. A long at the same time-remaining must
    still flow through the regular long-side bounds."""
    guard = PriceGuard()
    allowed, reason = guard.is_allowed(
        entry_price=30.0,
        direction="long",
        atr_regime="MEDIUM",
        time_remaining_sec=400,
    )
    assert allowed is True
    assert reason is None


def test_short_expiry_guard_runs_before_other_short_bounds():
    """A short with a too-cheap price AND inside the expiry window should
    surface SHORT_ENTRY_TOO_CLOSE_TO_EXPIRY first, not SHORT_ENTRY_TOO_CHEAP.
    Ordering matters for the operator's interpretation of skip_reason logs."""
    guard = PriceGuard()
    allowed, reason = guard.is_allowed(
        entry_price=10.0,
        direction="short",
        atr_regime="MEDIUM",
        time_remaining_sec=300,
    )
    assert allowed is False
    assert reason is not None
    assert reason.startswith("SHORT_ENTRY_TOO_CLOSE_TO_EXPIRY")


def test_short_expiry_guard_does_not_fire_when_remaining_unknown():
    """``time_remaining_sec is None`` means we don't know the expiry yet
    (typical right after a contract rotation). The blanket ``< 180s`` rule
    above only triggers when a concrete number is present, and so does
    this one. The coordinator's ``_is_near_expiry`` check is what handles
    the None case for entries."""
    guard = PriceGuard()
    allowed, reason = guard.is_allowed(
        entry_price=30.0,
        direction="short",
        atr_regime="MEDIUM",
        time_remaining_sec=None,
    )
    assert allowed is True
    assert reason is None
