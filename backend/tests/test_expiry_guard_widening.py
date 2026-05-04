"""Regression tests for BUG-032: orphan-on-close from too-narrow exit guard.

The 2026-05-04 live session produced 3 orphans (B79950, B80350, B79150)
all following the same pattern:

  T-50s   Coordinator's old 60s EXPIRY_GUARD trigger fires.
  T-30s   Position manager's first exit attempt sits in flight (Kalshi
          ack ~22s during pre-close volatility).
  T-10s   First retry. 2s backoff used up.
  T-2s    Second retry. 4s backoff used up.
  T+0s    Contract closes. 409 Conflict on next request.
  T+5s    coordinator.live_exit_abandoned + position_manager.orphan_adopted.

Worst-case pre-close exit attempt was ~70 seconds; the 60s window simply
isn't enough. This test battery enforces the wider 180s window AND the
short 5s fill-poll timeout that lets the position manager fail fast
during EXPIRY_GUARD/SHORT_SETTLEMENT_GUARD instead of sitting on the
default 15s poll loop.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from execution.position_manager import (
    PositionManager,
    PositionState,
    FILL_POLL_TIMEOUT,
)


# ══════════════════════════════════════════════════════════════════════
# BUG-032 layer 1: settings.bot.expiry_guard_trigger_sec
# ══════════════════════════════════════════════════════════════════════


class TestExpiryGuardTriggerWidening:

    def test_default_is_180_seconds_not_60(self):
        """Old hardcoded value was 60s. The single biggest contributor
        to today's orphan losses was 60s wasn't enough headroom for
        even one Kalshi exit-order round-trip during pre-close. New
        default is 180s so the worst-case retry sequence
        (2s + 4s backoff + 3 x ~22s requests = ~70s) still fits.
        """
        from config.settings import BotConfig
        cfg = BotConfig()
        assert cfg.expiry_guard_trigger_sec == 180

    def test_threshold_is_env_overridable(self):
        import os
        from config.settings import BotConfig
        prev = os.environ.get("EXPIRY_GUARD_TRIGGER_SEC")
        try:
            os.environ["EXPIRY_GUARD_TRIGGER_SEC"] = "240"
            cfg = BotConfig()
            assert cfg.expiry_guard_trigger_sec == 240
        finally:
            if prev is None:
                os.environ.pop("EXPIRY_GUARD_TRIGGER_SEC", None)
            else:
                os.environ["EXPIRY_GUARD_TRIGGER_SEC"] = prev

    def test_fill_poll_timeout_default_is_5_seconds(self):
        """Default 15s timeout is correct for normal exits but is too
        long when racing the contract close. We want to fail fast and
        retry within the wider 180s window."""
        from config.settings import BotConfig
        cfg = BotConfig()
        assert cfg.expiry_guard_fill_poll_timeout_sec == pytest.approx(5.0)

    def test_fill_poll_timeout_is_env_overridable(self):
        import os
        from config.settings import BotConfig
        prev = os.environ.get("EXPIRY_GUARD_FILL_POLL_TIMEOUT_SEC")
        try:
            os.environ["EXPIRY_GUARD_FILL_POLL_TIMEOUT_SEC"] = "3.5"
            cfg = BotConfig()
            assert cfg.expiry_guard_fill_poll_timeout_sec == pytest.approx(3.5)
        finally:
            if prev is None:
                os.environ.pop("EXPIRY_GUARD_FILL_POLL_TIMEOUT_SEC", None)
            else:
                os.environ["EXPIRY_GUARD_FILL_POLL_TIMEOUT_SEC"] = prev


# ══════════════════════════════════════════════════════════════════════
# BUG-032 layer 2: PositionManager._poll_order_fill timeout override
# ══════════════════════════════════════════════════════════════════════


class TestPollOrderFillTimeoutOverride:

    @pytest.mark.asyncio
    async def test_default_timeout_uses_module_constant(self):
        """No timeout_sec arg = use FILL_POLL_TIMEOUT (15s).
        Verify by patching the module-level constant down to 0.4s,
        then confirming the loop honored it (not a hardcoded value)."""
        client = MagicMock()
        client.get_order = AsyncMock(return_value={"order": {"status": "resting"}})
        pm = PositionManager(client)

        with patch("execution.position_manager.FILL_POLL_TIMEOUT", 0.4):
            order_data = await pm._poll_order_fill("order-X")

        assert order_data.get("status") == "resting"
        # 0.4s / 0.25s interval = roughly 1-2 polls before timeout.
        assert client.get_order.call_count >= 1
        assert client.get_order.call_count <= 3

    @pytest.mark.asyncio
    async def test_short_timeout_breaks_out_quickly(self):
        """When timeout_sec=0.5, the loop exits without waiting the
        full FILL_POLL_TIMEOUT. Use a non-terminal status so the loop
        only exits via the timeout, not via an early TERMINAL_STATUSES
        return."""
        client = MagicMock()
        client.get_order = AsyncMock(return_value={"order": {"status": "resting"}})
        pm = PositionManager(client)

        # Need real sleep semantics here so elapsed accumulates and the
        # while-clause can naturally fall through.
        order_data = await pm._poll_order_fill("order-Y", timeout_sec=0.5)
        # No fill, no terminal status, but the function returned because
        # elapsed >= 0.5. Proves the override is honored.
        assert order_data.get("status") == "resting"

    @pytest.mark.asyncio
    async def test_terminal_status_returns_immediately_regardless_of_timeout(self):
        """Even with a long timeout override, a terminal status short-
        circuits and returns immediately. Atomicity guarantee: a fill
        we observe is acted on, not delayed."""
        client = MagicMock()
        client.get_order = AsyncMock(return_value={
            "order": {"status": "executed", "yes_price": 50, "filled_quantity": 10}
        })
        pm = PositionManager(client)

        order_data = await pm._poll_order_fill("order-Z", timeout_sec=99999.0)
        assert order_data.get("status") == "executed"
        assert client.get_order.call_count == 1


# ══════════════════════════════════════════════════════════════════════
# BUG-032 layer 3: _exit_inner uses the short timeout for EXPIRY_GUARD
# ══════════════════════════════════════════════════════════════════════


class TestExitInnerHonorsExpiryReason:

    @pytest.mark.asyncio
    async def test_expiry_guard_passes_short_timeout_to_poll(self):
        """When _exit_inner is called with reason=EXPIRY_GUARD it must
        pass the short timeout (5s) to _poll_order_fill, not the
        default 15s. We can't call _exit_inner directly without mocking
        a ton of state; instead we verify the same plumbing by patching
        _poll_order_fill and checking the kwarg."""
        client = MagicMock()
        # Make order-creation succeed.
        client.create_order = AsyncMock(return_value={"order": {"order_id": "o1"}})
        client.get_order = AsyncMock(return_value={
            "order": {"status": "executed", "yes_price": 50, "filled_quantity": 5}
        })
        client.get_positions = AsyncMock(return_value={"market_positions": []})
        pm = PositionManager(client)

        from execution.position_manager import ManagedPosition
        pm.position = ManagedPosition(
            ticker="KXBTC-T", direction="long", contracts=5,
            entry_price=20.0, entry_time="2026-05-04T00:00:00+00:00",
            conviction="NORMAL", regime_at_entry="MEDIUM",
        )
        pm.state = PositionState.OPEN

        captured_kwargs = {}

        original = pm._poll_order_fill

        async def spy(*args, **kwargs):
            captured_kwargs.update(kwargs)
            # Return a terminal status to short-circuit the exit path.
            return {"status": "executed", "yes_price": 99, "filled_quantity": 5}

        pm._poll_order_fill = spy
        # Skip ledger verify by stubbing _verify_with_retry / verify
        async def _verify(*_a, **_kw):
            from execution.position_manager import VERIFY_FAILED  # noqa
            return 0
        pm.verify_position_on_exchange = _verify
        # Skip the 1.5s ledger-lag sleep
        with patch("execution.position_manager.asyncio.sleep", AsyncMock()):
            await pm._exit_inner(price=99.0, reason="EXPIRY_GUARD")
        # The whole point of BUG-032: EXPIRY_GUARD must NOT inherit the
        # default 15s polling window.
        assert captured_kwargs.get("timeout_sec") is not None
        assert captured_kwargs["timeout_sec"] < FILL_POLL_TIMEOUT, (
            "EXPIRY_GUARD must pass a shorter timeout to fail fast "
            f"during pre-close racing. Got {captured_kwargs}."
        )


# ══════════════════════════════════════════════════════════════════════
# BUG-032 layer 4: bg_persist_max bumped from 64 → 256 (env override)
# ══════════════════════════════════════════════════════════════════════


class TestBgPersistMaxConfigurable:

    def test_default_is_256(self):
        """64 was too low; cold-start storms saturate it inside 60s and
        trigger the healthcheck restart loop."""
        import os
        prev = os.environ.pop("BG_PERSIST_MAX", None)
        try:
            from coordinator import _bg_persist_max_env_default
            assert _bg_persist_max_env_default() == 256
        finally:
            if prev is not None:
                os.environ["BG_PERSIST_MAX"] = prev

    def test_env_override_honored(self):
        import os
        from coordinator import _bg_persist_max_env_default
        prev = os.environ.get("BG_PERSIST_MAX")
        try:
            os.environ["BG_PERSIST_MAX"] = "512"
            assert _bg_persist_max_env_default() == 512
        finally:
            if prev is None:
                os.environ.pop("BG_PERSIST_MAX", None)
            else:
                os.environ["BG_PERSIST_MAX"] = prev

    def test_invalid_env_falls_back_to_default(self):
        """Bad value (non-int, zero, negative) must not crash the bot
        on boot. Fall back to the safe default."""
        import os
        from coordinator import _bg_persist_max_env_default
        prev = os.environ.get("BG_PERSIST_MAX")
        try:
            for bad in ("not-an-int", "0", "-50"):
                os.environ["BG_PERSIST_MAX"] = bad
                assert _bg_persist_max_env_default() == 256, f"failed at {bad}"
        finally:
            if prev is None:
                os.environ.pop("BG_PERSIST_MAX", None)
            else:
                os.environ["BG_PERSIST_MAX"] = prev


# ══════════════════════════════════════════════════════════════════════
# Re-do test_normal_reason_uses_default_timeout properly
# ══════════════════════════════════════════════════════════════════════


class TestExitInnerNormalReasonDefaultTimeout:

    @pytest.mark.asyncio
    async def test_normal_reason_uses_default_timeout(self):
        """STOP_LOSS / TAKE_PROFIT / etc must use the default poll
        timeout (None = module default) so we don't accidentally make
        every exit fail-fast — only the racing-the-close case wants
        that behavior."""
        client = MagicMock()
        client.create_order = AsyncMock(return_value={"order": {"order_id": "o2"}})
        client.get_order = AsyncMock(return_value={
            "order": {"status": "executed", "yes_price": 50, "filled_quantity": 5}
        })
        client.get_positions = AsyncMock(return_value={"market_positions": []})
        pm = PositionManager(client)

        from execution.position_manager import ManagedPosition
        pm.position = ManagedPosition(
            ticker="KXBTC-T", direction="long", contracts=5,
            entry_price=20.0, entry_time="2026-05-04T00:00:00+00:00",
            conviction="NORMAL", regime_at_entry="MEDIUM",
        )
        pm.state = PositionState.OPEN

        captured_kwargs = {}

        async def spy(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return {"status": "executed", "yes_price": 99, "filled_quantity": 5}

        pm._poll_order_fill = spy

        async def _verify(*_a, **_kw):
            return 0
        pm.verify_position_on_exchange = _verify
        with patch("execution.position_manager.asyncio.sleep", AsyncMock()):
            await pm._exit_inner(price=99.0, reason="STOP_LOSS")
        # None means "use module default FILL_POLL_TIMEOUT".
        assert captured_kwargs.get("timeout_sec") is None
