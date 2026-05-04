"""Tests for the supervised live-trade cap.

Covers:
  * BotConfig.live_trade_limit defaults to 5 and reads LIVE_TRADE_LIMIT env.
  * PositionManager constructor honours an explicit ``live_trade_limit``.
  * ``can_enter`` flips False once the counter hits the limit.
  * ``restore_from_snapshot`` restores the counter (so a restart can't
    accidentally release the cap) but does NOT overwrite the limit set
    by the constructor (so changing the env is enough; no DB wipe).
  * LiveTrader passes the BotConfig value into PositionManager.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

# Disable real Kalshi auth — tests don't need a private key on disk.
os.environ.setdefault("LIVE_USE_FILL_STREAM", "false")

from execution.position_manager import PositionManager  # noqa: E402


# ─── BotConfig env wiring ─────────────────────────────────────────────


def _fresh_bot_config():
    """Construct BotConfig directly — bypasses the cached settings
    singleton so each test reads the current os.environ via the
    field default_factory."""
    from config.settings import BotConfig

    return BotConfig()


def test_bot_config_default_live_trade_limit_is_5(monkeypatch):
    monkeypatch.delenv("LIVE_TRADE_LIMIT", raising=False)
    cfg = _fresh_bot_config()
    assert cfg.live_trade_limit == 5


def test_bot_config_reads_live_trade_limit_env(monkeypatch):
    monkeypatch.setenv("LIVE_TRADE_LIMIT", "12")
    cfg = _fresh_bot_config()
    assert cfg.live_trade_limit == 12


def test_bot_config_zero_means_unlimited_intent(monkeypatch):
    """0 is the documented sentinel for 'unlimited'. The translation to
    None happens at the LiveTrader callsite (BotConfig stores the int
    verbatim so callers can distinguish 'env unset' from 'explicit 0')."""
    monkeypatch.setenv("LIVE_TRADE_LIMIT", "0")
    cfg = _fresh_bot_config()
    assert cfg.live_trade_limit == 0


# ─── PositionManager constructor + can_enter ──────────────────────────


def _make_pm(*, live_trade_limit=None):
    client = MagicMock()
    return PositionManager(client, live_trade_limit=live_trade_limit)


def test_pm_constructor_default_limit_is_none():
    pm = _make_pm()
    assert pm.live_trade_limit is None


def test_pm_constructor_honours_explicit_limit():
    pm = _make_pm(live_trade_limit=5)
    assert pm.live_trade_limit == 5


def test_can_enter_blocks_when_counter_reaches_limit():
    pm = _make_pm(live_trade_limit=5)
    pm._completed_live_trades = 5
    assert pm.can_enter is False


def test_can_enter_allows_when_under_limit():
    pm = _make_pm(live_trade_limit=5)
    pm._completed_live_trades = 4
    # FLAT + no orphans + no busy lock → eligible
    assert pm.can_enter is True


def test_can_enter_allows_when_limit_is_none():
    pm = _make_pm(live_trade_limit=None)
    pm._completed_live_trades = 9999
    assert pm.can_enter is True


def test_reset_trade_counter_clears_count_but_keeps_limit():
    pm = _make_pm(live_trade_limit=5)
    pm._completed_live_trades = 5
    assert pm.can_enter is False
    pm.reset_trade_counter()
    assert pm._completed_live_trades == 0
    assert pm.live_trade_limit == 5
    assert pm.can_enter is True


# ─── Snapshot restore preserves counter, not limit ────────────────────


def test_restore_from_snapshot_restores_counter():
    pm = _make_pm(live_trade_limit=5)
    pm.restore_from_snapshot({
        "state": "FLAT",
        "completed_live_trades": 3,
        # An older snapshot might persist a stale limit; we ignore it.
        "live_trade_limit": 99,
        "settled_tickers": [],
    })
    assert pm._completed_live_trades == 3
    # Limit is whatever the constructor said, NOT what the snapshot held.
    # This is the contract that lets operators bump LIVE_TRADE_LIMIT and
    # restart without first wiping bot_state.
    assert pm.live_trade_limit == 5


def test_restore_from_snapshot_handles_missing_counter():
    pm = _make_pm(live_trade_limit=5)
    pm.restore_from_snapshot({"state": "FLAT", "settled_tickers": []})
    # No counter in snapshot → keep whatever was already set (default 0).
    assert pm._completed_live_trades == 0


# ─── LiveTrader wiring ────────────────────────────────────────────────


def _make_live_trader_with_limit(limit_value):
    """Build a LiveTrader whose ``settings.bot.live_trade_limit`` returns
    ``limit_value``. ``settings`` is a module-level instance; ``bot`` is a
    frozen dataclass field on it, so we monkeypatch via
    ``object.__setattr__``. The patched value is restored at the end of
    the test by saving and reapplying the original."""
    from config import settings as _s

    original = _s.bot.live_trade_limit
    object.__setattr__(_s.bot, "live_trade_limit", limit_value)
    try:
        with patch("execution.live_trader.KalshiOrderClient") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            from execution.live_trader import LiveTrader

            sizer = MagicMock()
            lt = LiveTrader(sizer)
        return lt
    finally:
        object.__setattr__(_s.bot, "live_trade_limit", original)


def test_live_trader_passes_bot_config_limit_to_pm():
    """LiveTrader must read settings.bot.live_trade_limit and forward it
    to the PositionManager. A direct integration test against the real
    classes catches drift between the env wiring and the runtime path."""
    lt = _make_live_trader_with_limit(7)
    assert lt.position_manager.live_trade_limit == 7


def test_live_trader_translates_zero_to_unlimited():
    """LIVE_TRADE_LIMIT=0 must become live_trade_limit=None on the PM,
    not '0' (which would block all entries immediately)."""
    lt = _make_live_trader_with_limit(0)
    assert lt.position_manager.live_trade_limit is None


def test_live_trader_translates_negative_to_unlimited():
    """Defensive: anything <=0 collapses to None."""
    lt = _make_live_trader_with_limit(-3)
    assert lt.position_manager.live_trade_limit is None
