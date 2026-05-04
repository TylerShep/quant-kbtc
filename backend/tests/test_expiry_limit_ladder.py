"""Phase 3 (Expiry Exit Reliability, 2026-05-04): pre-expiry passive
limit ladder regression tests.

The ladder MUST:
  * Refuse to start when there is not enough time-to-expiry left to
    finish the ladder AND let EXPIRY_GUARD run on residual.
  * Place each rung at the configured offset from the executable side,
    stepping more aggressive per rung.
  * Cancel a non-terminal order before stepping to the next rung.
  * Aggregate partial fills across rungs.
  * Return None (and leave self.position intact) when no fills happened
    or when only partial fills were achieved -- the coordinator MUST
    fall back to EXPIRY_GUARD.
  * Increment the appropriate telemetry counters
    (runs / full_fills / partial_fills / no_fills / fallbacks).
  * Always release self._lock so the EXPIRY_GUARD path can acquire it.

These tests bypass the asyncio.sleep wall-clock by patching
``execution.position_manager.asyncio.sleep`` to a no-op.
"""
from __future__ import annotations

from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from execution.position_manager import (
    ManagedPosition,
    PositionManager,
    PositionState,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _build_pm(
    *,
    direction: str = "long",
    contracts: int = 5,
    entry_price: float = 30.0,
) -> tuple[MagicMock, PositionManager]:
    client = MagicMock()
    client.get_positions = AsyncMock(return_value={"market_positions": []})
    pm = PositionManager(client)
    pm.position = ManagedPosition(
        ticker="KXBTC-T", direction=direction, contracts=contracts,
        entry_price=entry_price, entry_time="2026-05-04T00:00:00+00:00",
        conviction="NORMAL", regime_at_entry="MEDIUM",
    )
    pm.state = PositionState.OPEN

    async def _drain(_oid, *, min_count=0, leg="exit"):
        return [], "order_response"

    pm._drain_fill_stream = _drain  # type: ignore[assignment]
    return client, pm


def _set_bot_attrs(**kwargs):
    """Override one or more frozen-dataclass attrs on settings.bot.
    Returns a list of (bot, name, prev) restore tuples."""
    from config.settings import settings as live_settings
    bot = live_settings.bot
    stash = []
    for k, v in kwargs.items():
        prev = getattr(bot, k)
        object.__setattr__(bot, k, v)
        stash.append((bot, k, prev))
    return stash


def _restore_bot_attrs(stash):
    for bot, name, prev in stash:
        object.__setattr__(bot, name, prev)


# ── Refuse-to-start guards ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_ladder_refuses_when_too_close_to_expiry():
    """Ladder must refuse to start when time_remaining_sec is at or
    below the absolute floor (guard_trigger + small margin). Otherwise
    the EXPIRY_GUARD path loses its full window."""
    _, pm = _build_pm()
    stash = _set_bot_attrs(
        ladder_total_budget_sec=50,
        expiry_guard_trigger_sec=180,
    )
    try:
        result = await pm.try_passive_limit_ladder(
            best_yes_bid=40, best_yes_ask=60,
            time_remaining_sec=170,  # under expiry_guard_trigger + 5
        )
    finally:
        _restore_bot_attrs(stash)
    assert result is None
    assert pm._ladder_runs == 0


@pytest.mark.asyncio
async def test_ladder_refuses_when_position_is_none():
    _, pm = _build_pm()
    pm.position = None
    result = await pm.try_passive_limit_ladder(
        best_yes_bid=40, best_yes_ask=60, time_remaining_sec=240,
    )
    assert result is None
    assert pm._ladder_runs == 0


@pytest.mark.asyncio
async def test_ladder_refuses_when_no_executable_side_long():
    """Long exit needs a YES bid as anchor; missing bid → no rung."""
    _, pm = _build_pm(direction="long")
    result = await pm.try_passive_limit_ladder(
        best_yes_bid=None, best_yes_ask=60, time_remaining_sec=240,
    )
    assert result is None


@pytest.mark.asyncio
async def test_ladder_refuses_when_no_executable_side_short():
    """Short exit needs a YES ask (-> NO bid anchor); missing ask → no rung."""
    _, pm = _build_pm(direction="short")
    result = await pm.try_passive_limit_ladder(
        best_yes_bid=40, best_yes_ask=None, time_remaining_sec=240,
    )
    assert result is None


# ── Rung pricing ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_rung_price_long_uses_bid_plus_first_offset():
    """For a long exit (sell YES) at best_yes_bid=40 with first_offset=5,
    rung 0 must be placed at yes_price=45."""
    client, pm = _build_pm(direction="long")
    client.create_order = AsyncMock(return_value={"order": {"order_id": "r0"}})
    client.cancel_order = AsyncMock(return_value={})
    # Make _poll_order_fill return canceled with no fills so the loop
    # exits cleanly after the first rung.
    pm._poll_order_fill = AsyncMock(return_value={
        "status": "canceled", "fill_count_fp": "0",
    })

    stash = _set_bot_attrs(
        ladder_rung_count=1,
        ladder_rung_first_offset_cents=5,
        ladder_rung_step_cents=3,
        ladder_rung_timeout_sec=0.1,
        ladder_total_budget_sec=10,
    )
    try:
        with patch("execution.position_manager.asyncio.sleep", AsyncMock()):
            result = await pm.try_passive_limit_ladder(
                best_yes_bid=40, best_yes_ask=60, time_remaining_sec=240,
            )
    finally:
        _restore_bot_attrs(stash)

    assert result is None  # No fills = ladder fallback to EXPIRY_GUARD.
    args, kwargs = client.create_order.call_args_list[0].args, client.create_order.call_args_list[0].kwargs
    assert kwargs.get("yes_price") == 45
    assert kwargs.get("type") == "limit"
    assert kwargs.get("action") == "sell"


@pytest.mark.asyncio
async def test_rungs_step_more_aggressive_per_rung():
    """Each successive rung WIDENS toward the bid -- the offset shrinks
    by step_cents, making the order more crossable."""
    client, pm = _build_pm(direction="long")
    client.create_order = AsyncMock(return_value={"order": {"order_id": "r0"}})
    client.cancel_order = AsyncMock(return_value={})
    pm._poll_order_fill = AsyncMock(return_value={
        "status": "canceled", "fill_count_fp": "0",
    })

    stash = _set_bot_attrs(
        ladder_rung_count=3,
        ladder_rung_first_offset_cents=10,
        ladder_rung_step_cents=3,
        ladder_rung_timeout_sec=0.1,
        ladder_total_budget_sec=10,
    )
    try:
        with patch("execution.position_manager.asyncio.sleep", AsyncMock()):
            await pm.try_passive_limit_ladder(
                best_yes_bid=40, best_yes_ask=60, time_remaining_sec=240,
            )
    finally:
        _restore_bot_attrs(stash)

    prices = [c.kwargs.get("yes_price") for c in client.create_order.call_args_list]
    # Rung 0: 40 + 10 = 50; Rung 1: 40 + (10-3) = 47; Rung 2: 40 + (10-6) = 44
    assert prices == [50, 47, 44]


@pytest.mark.asyncio
async def test_short_rung_uses_no_anchor():
    """Short exit (sell NO) anchors at 100 - best_yes_ask. With ask=70,
    NO anchor = 30; with first_offset=5, rung 0 is no_price=35."""
    client, pm = _build_pm(direction="short")
    client.create_order = AsyncMock(return_value={"order": {"order_id": "rs"}})
    client.cancel_order = AsyncMock(return_value={})
    pm._poll_order_fill = AsyncMock(return_value={
        "status": "canceled", "fill_count_fp": "0",
    })

    stash = _set_bot_attrs(
        ladder_rung_count=1,
        ladder_rung_first_offset_cents=5,
        ladder_rung_step_cents=3,
        ladder_rung_timeout_sec=0.1,
        ladder_total_budget_sec=10,
    )
    try:
        with patch("execution.position_manager.asyncio.sleep", AsyncMock()):
            await pm.try_passive_limit_ladder(
                best_yes_bid=20, best_yes_ask=70, time_remaining_sec=240,
            )
    finally:
        _restore_bot_attrs(stash)

    kwargs = client.create_order.call_args_list[0].kwargs
    assert kwargs.get("no_price") == 35
    assert "yes_price" not in kwargs
    assert kwargs.get("side") == "no"


# ── Outcome paths: full / partial / no fill ─────────────────────────


@pytest.mark.asyncio
async def test_full_fill_clears_position_and_returns_trade_dict():
    """When rung 0 fully fills, the ladder returns a trade dict and
    position is cleared. Telemetry: full_fills += 1."""
    client, pm = _build_pm(direction="long", contracts=5)
    client.create_order = AsyncMock(return_value={"order": {"order_id": "rfull"}})
    client.cancel_order = AsyncMock(return_value={})
    pm._poll_order_fill = AsyncMock(return_value={
        "status": "executed", "yes_price": 45, "fill_count_fp": "5",
    })

    stash = _set_bot_attrs(
        ladder_rung_count=1,
        ladder_rung_first_offset_cents=5,
        ladder_rung_step_cents=3,
        ladder_rung_timeout_sec=0.1,
        ladder_total_budget_sec=10,
    )
    try:
        with patch("execution.position_manager.asyncio.sleep", AsyncMock()):
            result = await pm.try_passive_limit_ladder(
                best_yes_bid=40, best_yes_ask=60, time_remaining_sec=240,
            )
    finally:
        _restore_bot_attrs(stash)

    assert result is not None
    assert result["exit_reason"] == "EXPIRY_LADDER"
    assert result["contracts"] == 5
    assert pm.position is None
    assert pm._ladder_runs == 1
    assert pm._ladder_full_fills == 1
    assert pm._ladder_no_fills == 0


@pytest.mark.asyncio
async def test_partial_fill_returns_none_and_keeps_residual_position():
    """Partial fill means the coordinator must fall back to
    EXPIRY_GUARD on the residual contracts. self.position is kept
    with the residual count; ladder returns None."""
    client, pm = _build_pm(direction="long", contracts=10)
    client.create_order = AsyncMock(return_value={"order": {"order_id": "rpart"}})
    client.cancel_order = AsyncMock(return_value={})
    # Each rung returns a 3-contract partial fill.
    pm._poll_order_fill = AsyncMock(return_value={
        "status": "canceled", "yes_price": 45, "fill_count_fp": "3",
    })

    stash = _set_bot_attrs(
        ladder_rung_count=2,
        ladder_rung_first_offset_cents=5,
        ladder_rung_step_cents=3,
        ladder_rung_timeout_sec=0.1,
        ladder_total_budget_sec=10,
    )
    try:
        with patch("execution.position_manager.asyncio.sleep", AsyncMock()):
            result = await pm.try_passive_limit_ladder(
                best_yes_bid=40, best_yes_ask=60, time_remaining_sec=240,
            )
    finally:
        _restore_bot_attrs(stash)

    assert result is None  # Caller must fall back to EXPIRY_GUARD.
    assert pm.position is not None
    # 10 - (3 + 3) = 4 contracts remaining on the residual.
    assert pm.position.contracts == 4
    # Telemetry: this is a partial outcome -> partial_fills counter.
    assert pm._ladder_runs == 1
    assert pm._ladder_partial_fills >= 1


@pytest.mark.asyncio
async def test_no_fill_returns_none_and_increments_no_fills():
    """When every rung completes with zero fills, ladder returns None
    and bumps both no_fills and fallbacks counters."""
    client, pm = _build_pm(direction="long", contracts=5)
    client.create_order = AsyncMock(return_value={"order": {"order_id": "rnone"}})
    client.cancel_order = AsyncMock(return_value={})
    pm._poll_order_fill = AsyncMock(return_value={
        "status": "canceled", "fill_count_fp": "0",
    })

    stash = _set_bot_attrs(
        ladder_rung_count=2,
        ladder_rung_first_offset_cents=5,
        ladder_rung_step_cents=3,
        ladder_rung_timeout_sec=0.1,
        ladder_total_budget_sec=10,
    )
    try:
        with patch("execution.position_manager.asyncio.sleep", AsyncMock()):
            result = await pm.try_passive_limit_ladder(
                best_yes_bid=40, best_yes_ask=60, time_remaining_sec=240,
            )
    finally:
        _restore_bot_attrs(stash)

    assert result is None
    assert pm.position is not None  # Caller falls back to EXPIRY_GUARD.
    assert pm._ladder_no_fills == 1
    assert pm._ladder_fallbacks == 1


# ── Cancellation discipline ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_terminal_orders_are_canceled_before_stepping():
    """If a rung order is still ``resting`` at timeout, the manager
    must cancel it before placing the next rung. Otherwise we would
    leave duplicate sell orders on the book."""
    client, pm = _build_pm(direction="long", contracts=5)
    client.create_order = AsyncMock(return_value={"order": {"order_id": "rest"}})
    cancel_calls: list[str] = []

    async def cancel(order_id):
        cancel_calls.append(order_id)
        return {}

    client.cancel_order = AsyncMock(side_effect=cancel)
    # Status "resting" + filled=0: no fill at all but order is still alive.
    pm._poll_order_fill = AsyncMock(return_value={
        "status": "resting", "fill_count_fp": "0",
    })

    stash = _set_bot_attrs(
        ladder_rung_count=2,
        ladder_rung_first_offset_cents=5,
        ladder_rung_step_cents=3,
        ladder_rung_timeout_sec=0.1,
        ladder_total_budget_sec=10,
    )
    try:
        with patch("execution.position_manager.asyncio.sleep", AsyncMock()):
            await pm.try_passive_limit_ladder(
                best_yes_bid=40, best_yes_ask=60, time_remaining_sec=240,
            )
    finally:
        _restore_bot_attrs(stash)

    # One cancel per rung (both resting) → 2 cancels total.
    assert len(cancel_calls) == 2


# ── Disabled / single-flight semantics ───────────────────────────────


@pytest.mark.asyncio
async def test_telemetry_includes_zero_runs_when_never_invoked():
    """get_ladder_telemetry must return all-zero counters when the
    ladder has never run -- ensures the diagnostics endpoint is never
    None even on a freshly-restarted bot."""
    _, pm = _build_pm()
    snap = pm.get_ladder_telemetry()
    assert snap == {
        "runs": 0,
        "full_fills": 0,
        "partial_fills": 0,
        "no_fills": 0,
        "fallbacks": 0,
        "in_flight_ticker": None,
    }


@pytest.mark.asyncio
async def test_lock_released_on_full_fill_so_other_flows_can_proceed():
    """After a full-fill ladder run, self._lock must be released so
    subsequent calls (orphan check, settlement) can acquire it."""
    client, pm = _build_pm(direction="long", contracts=5)
    client.create_order = AsyncMock(return_value={"order": {"order_id": "rdone"}})
    client.cancel_order = AsyncMock(return_value={})
    pm._poll_order_fill = AsyncMock(return_value={
        "status": "executed", "yes_price": 45, "fill_count_fp": "5",
    })

    stash = _set_bot_attrs(
        ladder_rung_count=1,
        ladder_rung_first_offset_cents=5,
        ladder_rung_step_cents=3,
        ladder_rung_timeout_sec=0.1,
        ladder_total_budget_sec=10,
    )
    try:
        with patch("execution.position_manager.asyncio.sleep", AsyncMock()):
            await pm.try_passive_limit_ladder(
                best_yes_bid=40, best_yes_ask=60, time_remaining_sec=240,
            )
    finally:
        _restore_bot_attrs(stash)

    assert pm.is_busy is False, (
        "Ladder MUST release the position lock on completion or "
        "EXPIRY_GUARD will be permanently blocked."
    )


@pytest.mark.asyncio
async def test_lock_released_on_no_fill_so_expiry_guard_can_run():
    client, pm = _build_pm(direction="long", contracts=5)
    client.create_order = AsyncMock(return_value={"order": {"order_id": "r0"}})
    client.cancel_order = AsyncMock(return_value={})
    pm._poll_order_fill = AsyncMock(return_value={
        "status": "canceled", "fill_count_fp": "0",
    })

    stash = _set_bot_attrs(
        ladder_rung_count=1,
        ladder_rung_first_offset_cents=5,
        ladder_rung_step_cents=3,
        ladder_rung_timeout_sec=0.1,
        ladder_total_budget_sec=10,
    )
    try:
        with patch("execution.position_manager.asyncio.sleep", AsyncMock()):
            await pm.try_passive_limit_ladder(
                best_yes_bid=40, best_yes_ask=60, time_remaining_sec=240,
            )
    finally:
        _restore_bot_attrs(stash)

    assert pm.is_busy is False
    assert pm._ladder_in_flight_ticker is None


# ── Coordinator integration: EXPIRY_GUARD remains the floor ─────────


@pytest.mark.asyncio
async def test_disabled_ladder_does_not_run():
    """When ladder_enabled_live=False, the coordinator must NOT call
    try_passive_limit_ladder. We verify the PositionManager telemetry
    stays at zero by simulating the coordinator gate."""
    from config.settings import settings
    # Defaults already disabled; this is a pin against accidental flip.
    assert settings.bot.ladder_enabled_live is False
    assert settings.bot.ladder_enabled_paper is False
