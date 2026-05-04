"""Tests for the supervised auto-pause behavior on live trade exits.

The supervised auto-pause is gated behind ``BotConfig.supervised_auto_pause``
(env: ``SUPERVISED_AUTO_PAUSE``). Default OFF — operators rely on the
``live_trade_limit`` cap and manual dashboard pauses instead.

These tests use a minimal stand-in for ``Coordinator`` rather than the full
class because Coordinator pulls in WebSockets, DB pools, and Kalshi auth
that we don't need to exercise here. The test double mirrors exactly the
attributes ``_on_trade_exit`` reads/writes.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("LIVE_USE_FILL_STREAM", "false")


# ─── BotConfig env wiring ─────────────────────────────────────────────


def _fresh_bot_config():
    """Construct BotConfig directly — bypasses the cached settings
    singleton so each test reads the current os.environ via the
    field default_factory."""
    from config.settings import BotConfig

    return BotConfig()


def test_default_supervised_auto_pause_is_off(monkeypatch):
    """Default must be OFF so live trading runs continuously after
    the post-OOM stabilisation. If we ever flip the default back on,
    this test will catch it and force an explicit decision."""
    monkeypatch.delenv("SUPERVISED_AUTO_PAUSE", raising=False)
    cfg = _fresh_bot_config()
    assert cfg.supervised_auto_pause is False


def test_supervised_auto_pause_reads_env_true(monkeypatch):
    monkeypatch.setenv("SUPERVISED_AUTO_PAUSE", "true")
    cfg = _fresh_bot_config()
    assert cfg.supervised_auto_pause is True


def test_supervised_auto_pause_reads_env_false(monkeypatch):
    monkeypatch.setenv("SUPERVISED_AUTO_PAUSE", "false")
    cfg = _fresh_bot_config()
    assert cfg.supervised_auto_pause is False


# ─── _on_trade_exit gating ────────────────────────────────────────────


class _FakeTrade:
    def __init__(self, ticker="KXBTC-T", exit_reason="STOP_LOSS", pnl=-0.13):
        self.ticker = ticker
        self.exit_reason = exit_reason
        self.pnl = pnl


def _bind_on_trade_exit_to_stub(stub):
    """Bind the real Coordinator._on_trade_exit method to a lightweight
    stub object that exposes only the attributes the method touches.
    This avoids the heavy Coordinator.__init__ (DB pool, FillStream,
    Kalshi auth) while still exercising the production code path."""
    from coordinator import Coordinator

    return Coordinator._on_trade_exit.__get__(stub, Coordinator)


def _make_stub():
    """Return a stub with exactly the attributes _on_trade_exit reads."""
    stub = MagicMock()
    stub._tick_count = 100
    stub._last_live_exit_tick = 0
    stub._last_paper_exit_tick = 0
    stub.trading_paused = "off"
    return stub


def _set_pause_flag(value: bool):
    """Patch settings.bot.supervised_auto_pause on the live singleton.
    BotConfig is a frozen dataclass so we use object.__setattr__ to
    mutate it; the caller restores via try/finally."""
    from config import settings as _s

    original = _s.bot.supervised_auto_pause
    object.__setattr__(_s.bot, "supervised_auto_pause", value)
    return original


def _restore_pause_flag(original: bool):
    from config import settings as _s

    object.__setattr__(_s.bot, "supervised_auto_pause", original)


def _run_on_trade_exit(stub, trade, mode: str):
    """Invoke the bound _on_trade_exit. The method calls
    ``asyncio.create_task()`` which requires a running loop, so we
    drive the call from inside ``loop.run_until_complete`` of an
    awaitable wrapper."""
    on_trade_exit = _bind_on_trade_exit_to_stub(stub)

    async def _runner():
        # Patch ws_manager.broadcast and the persist coroutine so they
        # don't fire real network/DB calls. They must each return a
        # fresh awaitable per invocation (asyncio.sleep(0)) — a single
        # cached coroutine object can only be awaited once.
        with patch("coordinator.ws_manager") as mock_ws, \
             patch.object(
                 stub,
                 "_persist_and_notify_exit",
                 side_effect=lambda *a, **kw: asyncio.sleep(0),
             ):
            mock_ws.broadcast = MagicMock(side_effect=lambda *a, **kw: asyncio.sleep(0))
            on_trade_exit(trade, "BTC", mode)
            # Yield once so spawned tasks get a chance to run, then
            # drain anything still pending so the loop closes cleanly.
            await asyncio.sleep(0)
            pending = [t for t in asyncio.all_tasks() if not t.done()
                       and t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    # Save and restore the existing loop so we don't poison sibling
    # tests that rely on the pytest-asyncio global loop being usable.
    try:
        prior = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        prior = None
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_runner())
    finally:
        loop.close()
        if prior is not None and not prior.is_closed():
            asyncio.set_event_loop(prior)


def test_live_exit_does_not_pause_when_flag_off():
    stub = _make_stub()
    original = _set_pause_flag(False)
    try:
        _run_on_trade_exit(stub, _FakeTrade(), mode="live")
        assert stub.trading_paused == "off", (
            "Live exit must NOT auto-pause when SUPERVISED_AUTO_PAUSE=false"
        )
        assert stub._last_live_exit_tick == 100
    finally:
        _restore_pause_flag(original)


def test_live_exit_pauses_when_flag_on():
    stub = _make_stub()
    original = _set_pause_flag(True)
    try:
        _run_on_trade_exit(stub, _FakeTrade(), mode="live")
        assert stub.trading_paused == "paused", (
            "Live exit MUST auto-pause when SUPERVISED_AUTO_PAUSE=true"
        )
        assert stub._last_live_exit_tick == 100
    finally:
        _restore_pause_flag(original)


def test_paper_exit_never_pauses_regardless_of_flag():
    """The supervised pause is live-only by design. Paper trading must
    never be interrupted by it (paper is the data-collection lane)."""
    for flag_value in (True, False):
        stub = _make_stub()
        original = _set_pause_flag(flag_value)
        try:
            _run_on_trade_exit(stub, _FakeTrade(), mode="paper")
            assert stub.trading_paused == "off"
            assert stub._last_paper_exit_tick == 100
            assert stub._last_live_exit_tick == 0, (
                "Paper exit must not touch the live exit-tick counter"
            )
        finally:
            _restore_pause_flag(original)


def test_live_exit_with_flag_off_does_not_emit_pause_log(caplog):
    """When the flag is off, the supervised_auto_pause log line must
    NOT appear — operators rely on log volume to spot anomalies."""
    import logging

    stub = _make_stub()
    original = _set_pause_flag(False)
    try:
        with caplog.at_level(logging.INFO, logger="coordinator"):
            _run_on_trade_exit(stub, _FakeTrade(), mode="live")
        assert not any(
            "supervised_auto_pause" in (r.getMessage() or "")
            for r in caplog.records
        ), "Pause log fired despite flag being off"
    finally:
        _restore_pause_flag(original)
