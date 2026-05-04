"""Phase 2 (Expiry Exit Reliability, 2026-05-04): coordinator retry
behavior for live EXPIRY_GUARD / SHORT_SETTLEMENT_GUARD exits.

Validates that ``Coordinator._handle_live_exit`` correctly:
  * Forwards the 1-based retry attempt number into
    ``LiveTrader.exit(..., attempt=N)`` so PositionManager can pick
    the right widening floor.
  * Uses the configurable backoff schedule (``expiry_retry_*``) and
    falls back to the legacy 2s + 4s sequence with default config.
  * Honors ``expiry_retry_max_attempts`` for the total number of
    tries.
  * Preserves the existing orphan-adoption fallback when all retries
    fail and the position is still on the exchange.
  * Always clears ``_live_exit_in_flight`` in the ``finally`` block,
    even when an exception bubbles up from a retry.

The tests build a minimal Coordinator without touching real Kalshi
keys or asyncio.sleep wall-clock time.
"""
from __future__ import annotations

from typing import List, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coordinator import Coordinator


# ── Helpers ──────────────────────────────────────────────────────────


def _make_coordinator() -> Coordinator:
    """Build a Coordinator with mocked dependencies for retry tests."""
    with patch("execution.live_trader.KalshiOrderClient"), \
         patch("data.fill_stream.KalshiAuth"), \
         patch("notifications.get_notifier") as mock_notifier:
        mock_notifier.return_value = MagicMock(
            unhandled_exception=AsyncMock(),
            db_error=AsyncMock(),
            ws_disconnected=AsyncMock(),
        )
        coord = Coordinator()
    coord.live_trader = MagicMock()
    coord.live_trader.has_position = True
    coord.live_trader.position = MagicMock(
        ticker="KXBTC-T", direction="long", contracts=5, entry_price=20.0,
    )
    coord.live_trader.position_manager = MagicMock()
    coord.live_trader.position_manager.is_busy = False
    coord.live_trader.position_manager.adopt_orphan_and_clear_position = (
        MagicMock()
    )
    coord._on_trade_exit = MagicMock()
    coord._unregister_position_ticker = MagicMock()
    coord._live_exit_in_flight = True
    return coord


def _set_bot_attr(name: str, value):
    """Temporarily override a frozen-dataclass field on settings.bot."""
    from config.settings import settings as live_settings
    bot = live_settings.bot
    prev = getattr(bot, name)
    object.__setattr__(bot, name, value)
    return bot, name, prev


def _restore_bot_attr(stash):
    bot, name, prev = stash
    object.__setattr__(bot, name, prev)


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_attempt_succeeds_no_retries():
    """When the original exit returns a Trade, no retry is fired and
    _live_exit_in_flight is cleared."""
    coord = _make_coordinator()

    fake_trade = MagicMock(ticker="KXBTC-T", exit_reason="EXPIRY_GUARD",
                            pnl=1.0, candles_held=2)

    async def trade_future():
        return fake_trade

    await coord._handle_live_exit(
        trade_future(), "BTC",
        original_reason="EXPIRY_GUARD", exit_price=99.0,
    )

    coord._on_trade_exit.assert_called_once_with(fake_trade, "BTC", "live")
    coord.live_trader.exit.assert_not_called()
    assert coord._live_exit_in_flight is False


@pytest.mark.asyncio
async def test_retry_attempt_numbers_are_passed_into_live_trader_exit():
    """When the original exit returns None and the position is still
    open, retries must call live_trader.exit with attempt=1, then
    attempt=2 (with default max_attempts=3)."""
    coord = _make_coordinator()
    coord.live_trader.exit = AsyncMock(return_value=None)

    async def trade_future():
        return None

    with patch("coordinator.asyncio.sleep", AsyncMock()):
        await coord._handle_live_exit(
            trade_future(), "BTC",
            original_reason="EXPIRY_GUARD", exit_price=99.0,
        )

    # Default max_attempts=3 → original + 2 retries.
    assert coord.live_trader.exit.call_count == 2
    attempts = [c.kwargs.get("attempt") for c in coord.live_trader.exit.call_args_list]
    assert attempts == [1, 2], (
        "Coordinator must pass 1-based retry index into live_trader.exit "
        "so position_manager can pick the right widening floor."
    )


@pytest.mark.asyncio
async def test_retry_succeeds_clears_in_flight():
    """If retry #1 returns a trade, the loop short-circuits and the
    in-flight flag is cleared."""
    coord = _make_coordinator()
    fake_trade = MagicMock(ticker="KXBTC-T", exit_reason="EXPIRY_GUARD",
                            pnl=2.0, candles_held=2)
    coord.live_trader.exit = AsyncMock(return_value=fake_trade)

    async def trade_future():
        return None

    with patch("coordinator.asyncio.sleep", AsyncMock()):
        await coord._handle_live_exit(
            trade_future(), "BTC",
            original_reason="EXPIRY_GUARD", exit_price=99.0,
        )

    assert coord.live_trader.exit.call_count == 1
    coord._on_trade_exit.assert_called_once_with(fake_trade, "BTC", "live")
    assert coord._live_exit_in_flight is False


@pytest.mark.asyncio
async def test_all_retries_fail_position_adopted_as_orphan():
    """If every retry returns None and the position is still on the
    exchange, the existing orphan-adoption fallback fires and the
    in-flight flag is still cleared."""
    coord = _make_coordinator()
    coord.live_trader.exit = AsyncMock(return_value=None)

    async def trade_future():
        return None

    with patch("coordinator.asyncio.sleep", AsyncMock()):
        await coord._handle_live_exit(
            trade_future(), "BTC",
            original_reason="EXPIRY_GUARD", exit_price=99.0,
        )

    assert coord._live_exit_in_flight is False
    coord.live_trader.position_manager.adopt_orphan_and_clear_position.assert_called_once()
    coord._on_trade_exit.assert_not_called()


@pytest.mark.asyncio
async def test_retry_exception_is_logged_and_loop_continues():
    """If a retry raises, the exception is caught and the loop tries
    the NEXT retry. After all retries fail, the position is adopted."""
    coord = _make_coordinator()

    async def _raising_exit(price, reason, attempt=0):
        raise RuntimeError(f"transient {attempt}")

    coord.live_trader.exit = AsyncMock(side_effect=_raising_exit)

    async def trade_future():
        return None

    with patch("coordinator.asyncio.sleep", AsyncMock()):
        await coord._handle_live_exit(
            trade_future(), "BTC",
            original_reason="EXPIRY_GUARD", exit_price=99.0,
        )

    assert coord.live_trader.exit.call_count == 2
    assert coord._live_exit_in_flight is False
    coord.live_trader.position_manager.adopt_orphan_and_clear_position.assert_called_once()


@pytest.mark.asyncio
async def test_in_flight_flag_clears_when_outer_exception_raised():
    """Even if the awaited future itself raises, _live_exit_in_flight
    must be cleared in the finally block."""
    coord = _make_coordinator()

    async def trade_future():
        raise RuntimeError("kaboom")

    await coord._handle_live_exit(
        trade_future(), "BTC",
        original_reason="EXPIRY_GUARD", exit_price=99.0,
    )

    assert coord._live_exit_in_flight is False


@pytest.mark.asyncio
async def test_position_disappears_mid_retry_short_circuits():
    """If has_position flips to False between retries (e.g. because
    the previous attempt actually filled and just didn't return a
    trade dict), no further retries are issued and no orphan is
    adopted."""
    coord = _make_coordinator()
    call_count = {"n": 0}

    def _set_has_position_after_first_call(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] >= 1:
            coord.live_trader.has_position = False
        return None

    coord.live_trader.exit = AsyncMock(side_effect=_set_has_position_after_first_call)

    async def trade_future():
        return None

    with patch("coordinator.asyncio.sleep", AsyncMock()):
        await coord._handle_live_exit(
            trade_future(), "BTC",
            original_reason="EXPIRY_GUARD", exit_price=99.0,
        )

    # Exactly one retry fired before has_position flipped off.
    assert coord.live_trader.exit.call_count == 1
    coord.live_trader.position_manager.adopt_orphan_and_clear_position.assert_not_called()
    assert coord._live_exit_in_flight is False


@pytest.mark.asyncio
async def test_max_attempts_one_means_no_retries():
    """expiry_retry_max_attempts=1 → original try only, no retries.
    Ensures the configurable max-attempts path is honored."""
    coord = _make_coordinator()
    coord.live_trader.exit = AsyncMock(return_value=None)

    async def trade_future():
        return None

    stash = _set_bot_attr("expiry_retry_max_attempts", 1)
    try:
        with patch("coordinator.asyncio.sleep", AsyncMock()):
            await coord._handle_live_exit(
                trade_future(), "BTC",
                original_reason="EXPIRY_GUARD", exit_price=99.0,
            )
    finally:
        _restore_bot_attr(stash)

    assert coord.live_trader.exit.call_count == 0, (
        "max_attempts=1 means the original try is the only try -- no retries."
    )
    coord.live_trader.position_manager.adopt_orphan_and_clear_position.assert_called_once()


@pytest.mark.asyncio
async def test_backoff_schedule_uses_config_base():
    """With backoff_base=3 and max_backoff large, retry sleeps should
    be 3, 9 (3**1, 3**2). Validates the new configurable schedule
    replaces the historical hardcoded 2**N."""
    coord = _make_coordinator()
    coord.live_trader.exit = AsyncMock(return_value=None)

    async def trade_future():
        return None

    sleep_calls: List[float] = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    stash_base = _set_bot_attr("expiry_retry_backoff_base_sec", 3.0)
    stash_max = _set_bot_attr("expiry_retry_max_backoff_sec", 100.0)
    try:
        with patch("coordinator.asyncio.sleep", fake_sleep):
            await coord._handle_live_exit(
                trade_future(), "BTC",
                original_reason="EXPIRY_GUARD", exit_price=99.0,
            )
    finally:
        _restore_bot_attr(stash_base)
        _restore_bot_attr(stash_max)

    # Default max_attempts=3 → 2 retries → 2 sleep calls before each retry.
    assert sleep_calls == [3.0, 9.0]


@pytest.mark.asyncio
async def test_backoff_clamped_to_max_backoff():
    """When base ** N exceeds max_backoff, the sleep is clamped. This
    keeps a misconfigured base from extending the orphan window."""
    coord = _make_coordinator()
    coord.live_trader.exit = AsyncMock(return_value=None)

    async def trade_future():
        return None

    sleep_calls: List[float] = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    stash_base = _set_bot_attr("expiry_retry_backoff_base_sec", 5.0)
    stash_max = _set_bot_attr("expiry_retry_max_backoff_sec", 6.0)
    try:
        with patch("coordinator.asyncio.sleep", fake_sleep):
            await coord._handle_live_exit(
                trade_future(), "BTC",
                original_reason="EXPIRY_GUARD", exit_price=99.0,
            )
    finally:
        _restore_bot_attr(stash_base)
        _restore_bot_attr(stash_max)

    # 5**1 = 5 (under cap), 5**2 = 25 (clamped to 6).
    assert sleep_calls == [5.0, 6.0]


@pytest.mark.asyncio
async def test_default_backoff_schedule_matches_legacy_two_then_four():
    """Default expiry_retry_backoff_base_sec=2, max_backoff=8 → sleeps
    [2, 4]. This is the legacy hardcoded schedule -- pin it so a
    config refactor can't silently shorten the window."""
    coord = _make_coordinator()
    coord.live_trader.exit = AsyncMock(return_value=None)

    async def trade_future():
        return None

    sleep_calls: List[float] = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    with patch("coordinator.asyncio.sleep", fake_sleep):
        await coord._handle_live_exit(
            trade_future(), "BTC",
            original_reason="EXPIRY_GUARD", exit_price=99.0,
        )

    assert sleep_calls == [2.0, 4.0]


@pytest.mark.asyncio
async def test_short_settlement_guard_attempt_propagation():
    """SHORT_SETTLEMENT_GUARD must use the same retry plumbing -- both
    reasons share the position_manager widening logic."""
    coord = _make_coordinator()
    coord.live_trader.position.direction = "short"
    coord.live_trader.exit = AsyncMock(return_value=None)

    async def trade_future():
        return None

    with patch("coordinator.asyncio.sleep", AsyncMock()):
        await coord._handle_live_exit(
            trade_future(), "BTC",
            original_reason="SHORT_SETTLEMENT_GUARD", exit_price=70.0,
        )

    assert coord.live_trader.exit.call_count == 2
    attempts = [c.kwargs.get("attempt") for c in coord.live_trader.exit.call_args_list]
    reasons = [c.args[1] for c in coord.live_trader.exit.call_args_list]
    assert attempts == [1, 2]
    assert all(r == "SHORT_SETTLEMENT_GUARD" for r in reasons)
