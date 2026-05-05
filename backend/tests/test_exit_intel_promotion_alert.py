"""Tests for the one-shot Discord alert that fires when paper telemetry
coverage is rich enough to evaluate promoting the health-score from
shadow mode to an actual exit driver.

The alert is wired into ``Coordinator._persist_and_notify_exit`` (paper
branch) and gated behind:

  * ``EXIT_INTELLIGENCE_ENABLED`` (must be on)
  * ``EXIT_INTELLIGENCE_SHADOW_ONLY`` (must still be on — promoted means
    no further alerts)
  * ``POSITION_TELEMETRY_ENABLED``

It must:
  1. Skip silently when paper coverage is below thresholds.
  2. Fire exactly once when thresholds are met, then latch via
     ``_exit_intel_promotion_sent`` so subsequent paper exits don't
     re-spam the channel.
  3. Persist the latch flag in ``bot_state`` so a restart preserves it.

These tests use a fake asyncpg-style pool that returns canned aggregate
rows for the promotion SQL and records every other write so we can
verify the latch survives the persist round-trip.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _arun(coro):
    """Run a coroutine on a private loop, drain spawned tasks, and
    restore the prior loop. Mirrors test_paper_position_persist."""
    loop = asyncio.new_event_loop()
    prev = None
    try:
        try:
            prev = asyncio.get_event_loop()
        except RuntimeError:
            prev = None
        asyncio.set_event_loop(loop)

        async def _wrapper():
            result = await coro
            pending = [
                t for t in asyncio.all_tasks()
                if not t.done() and t is not asyncio.current_task()
            ]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            return result

        return loop.run_until_complete(_wrapper())
    finally:
        loop.close()
        if prev is not None and not prev.is_closed():
            asyncio.set_event_loop(prev)
        else:
            asyncio.set_event_loop(asyncio.new_event_loop())


def _make_coordinator():
    """Construct a Coordinator with auth/network classes stubbed."""
    with patch("execution.live_trader.KalshiOrderClient"), \
         patch("data.fill_stream.KalshiAuth"), \
         patch("data.fill_stream.KalshiOrderClient", create=True), \
         patch("execution.position_manager.KalshiOrderClient", create=True), \
         patch("notifications.get_notifier"):
        from coordinator import Coordinator
        return Coordinator()


class _FakeCursor:
    def __init__(self, row_to_return=None):
        self._row = row_to_return

    async def fetchone(self):
        return self._row


class _FakeConn:
    """Routes the promotion-aggregate SELECT to a configured row, and
    records bot_state INSERTs so the test can assert the latch flag was
    persisted."""

    def __init__(self, store: dict, agg_row: tuple):
        self._store = store
        self._agg_row = agg_row
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, params: tuple = ()) -> _FakeCursor:
        self.executed.append((sql, params))
        sql_lower = sql.lower()
        if "with trade_telemetry as" in sql_lower:
            return _FakeCursor(self._agg_row)
        if "select value from bot_state" in sql_lower and "sizer_state" in sql_lower:
            row = self._store.get("sizer_state")
            return _FakeCursor((row,) if row is not None else None)
        if "select value from bot_state" in sql_lower and "param_overrides" in sql_lower:
            return _FakeCursor(None)
        if "insert into bot_state" in sql_lower and params:
            try:
                payload = json.loads(params[0])
                if "paper" in payload and "live" in payload:
                    self._store["sizer_state"] = payload
            except (TypeError, ValueError):
                pass
            return _FakeCursor()
        if "select" in sql_lower and "trades" in sql_lower:
            return _FakeCursor((0,))
        return _FakeCursor()


class _FakePool:
    def __init__(self, agg_row: tuple):
        self.store: dict = {}
        self.agg_row = agg_row
        self.last_conn: _FakeConn | None = None

    def connection(self):
        pool = self

        class _Ctx:
            async def __aenter__(self_inner):
                pool.last_conn = _FakeConn(pool.store, pool.agg_row)
                return pool.last_conn

            async def __aexit__(self_inner, *a):
                return False

        return _Ctx()


def _set(name: str, value):
    """Mutate the frozen BotConfig singleton for a test, returning the
    prior value so the caller can restore in finally."""
    from config import settings as _s
    original = getattr(_s.bot, name)
    object.__setattr__(_s.bot, name, value)
    return original


def _restore(name: str, value):
    from config import settings as _s
    object.__setattr__(_s.bot, name, value)


# ─── Settings wiring ──────────────────────────────────────────────────


def test_promotion_thresholds_default_to_safe_floor(monkeypatch):
    """Defaults must be conservative — fires only after enough cross-
    regime cross-session paper telemetry is on hand. If we ever lower
    the floor, this test forces an explicit decision."""
    for k in (
        "EXIT_INTEL_PROMOTION_MIN_PAPER_TRADES",
        "EXIT_INTEL_PROMOTION_MIN_REGIMES",
        "EXIT_INTEL_PROMOTION_MIN_HOURS",
    ):
        monkeypatch.delenv(k, raising=False)
    from config.settings import BotConfig
    cfg = BotConfig()
    assert cfg.exit_intel_promotion_min_paper_trades >= 100
    assert cfg.exit_intel_promotion_min_distinct_regimes >= 2
    assert cfg.exit_intel_promotion_min_distinct_hours >= 6


# ─── Notifier shape ───────────────────────────────────────────────────


def test_notifier_promotion_alert_handles_none_score_split():
    """Avg-min-score columns are nullable when no winners (or losers)
    have telemetry coverage. The notifier must format that gracefully
    rather than blowing up the post-paper-exit hook."""
    from notifications import DiscordNotifier

    notifier = DiscordNotifier(attribution_url="")
    captured: dict[str, Any] = {}

    async def _fake_post(url: str, embed: dict) -> None:
        captured["url"] = url
        captured["embed"] = embed

    with patch.object(notifier, "_post", side_effect=_fake_post):
        _arun(notifier.exit_intelligence_promotion_ready(
            qualifying_trades=120,
            distinct_regimes=2,
            distinct_hours=8,
            winners_with_telemetry=70,
            losers_with_telemetry=50,
            avg_min_score_winners=None,
            avg_min_score_losers=None,
            current_threshold=35.0,
            breach_ticks=3,
        ))

    embed = captured["embed"]
    assert "Promotion-Ready Review" in embed["title"]
    field_names = {f["name"] for f in embed["fields"]}
    assert "Qualifying Paper Trades" in field_names
    assert "Avg Min Score (Win)" in field_names
    score_field = next(f for f in embed["fields"] if f["name"] == "Avg Min Score (Win)")
    assert score_field["value"] == "N/A"


def test_notifier_promotion_alert_includes_winloss_spread():
    from notifications import DiscordNotifier

    notifier = DiscordNotifier(attribution_url="")
    captured: dict[str, Any] = {}

    async def _fake_post(url: str, embed: dict) -> None:
        captured["embed"] = embed

    with patch.object(notifier, "_post", side_effect=_fake_post):
        _arun(notifier.exit_intelligence_promotion_ready(
            qualifying_trades=140,
            distinct_regimes=3,
            distinct_hours=10,
            winners_with_telemetry=80,
            losers_with_telemetry=60,
            avg_min_score_winners=62.0,
            avg_min_score_losers=41.0,
            current_threshold=35.0,
            breach_ticks=3,
        ))

    spread_field = next(
        f for f in captured["embed"]["fields"] if f["name"].startswith("Win")
        and "Spread" in f["name"]
    )
    assert "+21.0" in spread_field["value"]


# ─── Coordinator threshold check ──────────────────────────────────────


def _coordinator_with_pool(agg_row: tuple):
    coord = _make_coordinator()
    pool = _FakePool(agg_row)
    coord._pool = pool
    return coord, pool


def test_promotion_check_does_not_fire_when_below_min_trades():
    coord, _ = _coordinator_with_pool(
        agg_row=(50, 2, 8, 30, 20, 60.0, 40.0)  # qualifying_trades < 100
    )
    fake_notifier = MagicMock()
    fake_notifier.exit_intelligence_promotion_ready = AsyncMock()
    with patch("coordinator.get_notifier", return_value=fake_notifier):
        _arun(coord._check_exit_intelligence_promotion_threshold())
    fake_notifier.exit_intelligence_promotion_ready.assert_not_called()
    assert coord._exit_intel_promotion_sent is False


def test_promotion_check_does_not_fire_when_only_one_regime_seen():
    coord, _ = _coordinator_with_pool(
        agg_row=(150, 1, 8, 80, 70, 60.0, 40.0)  # distinct_regimes < 2
    )
    fake_notifier = MagicMock()
    fake_notifier.exit_intelligence_promotion_ready = AsyncMock()
    with patch("coordinator.get_notifier", return_value=fake_notifier):
        _arun(coord._check_exit_intelligence_promotion_threshold())
    fake_notifier.exit_intelligence_promotion_ready.assert_not_called()
    assert coord._exit_intel_promotion_sent is False


def test_promotion_check_does_not_fire_when_hour_coverage_insufficient():
    coord, _ = _coordinator_with_pool(
        agg_row=(200, 3, 3, 110, 90, 65.0, 40.0)  # distinct_hours < 6
    )
    fake_notifier = MagicMock()
    fake_notifier.exit_intelligence_promotion_ready = AsyncMock()
    with patch("coordinator.get_notifier", return_value=fake_notifier):
        _arun(coord._check_exit_intelligence_promotion_threshold())
    fake_notifier.exit_intelligence_promotion_ready.assert_not_called()
    assert coord._exit_intel_promotion_sent is False


def test_promotion_check_fires_and_latches_when_thresholds_met():
    coord, pool = _coordinator_with_pool(
        agg_row=(140, 2, 9, 80, 60, 62.0, 41.0)
    )
    fake_notifier = MagicMock()
    fake_notifier.exit_intelligence_promotion_ready = AsyncMock()
    with patch("coordinator.get_notifier", return_value=fake_notifier):
        _arun(coord._check_exit_intelligence_promotion_threshold())

    fake_notifier.exit_intelligence_promotion_ready.assert_called_once()
    kwargs = fake_notifier.exit_intelligence_promotion_ready.call_args.kwargs
    assert kwargs["qualifying_trades"] == 140
    assert kwargs["distinct_regimes"] == 2
    assert kwargs["distinct_hours"] == 9
    assert kwargs["winners_with_telemetry"] == 80
    assert kwargs["losers_with_telemetry"] == 60
    assert kwargs["avg_min_score_winners"] == pytest.approx(62.0)
    assert kwargs["avg_min_score_losers"] == pytest.approx(41.0)

    assert coord._exit_intel_promotion_sent is True
    saved = pool.store.get("sizer_state")
    assert saved is not None
    assert saved.get("exit_intel_promotion_sent") is True


def test_promotion_check_handles_null_score_columns():
    """Avg score columns are NULL when one of the win/loss buckets is
    empty. Coordinator must not coerce that into 0.0 (which would falsely
    suggest the gate is uninformative) and must not crash."""
    coord, _ = _coordinator_with_pool(
        agg_row=(120, 2, 8, 120, 0, 60.0, None)
    )
    fake_notifier = MagicMock()
    fake_notifier.exit_intelligence_promotion_ready = AsyncMock()
    with patch("coordinator.get_notifier", return_value=fake_notifier):
        _arun(coord._check_exit_intelligence_promotion_threshold())

    fake_notifier.exit_intelligence_promotion_ready.assert_called_once()
    kwargs = fake_notifier.exit_intelligence_promotion_ready.call_args.kwargs
    assert kwargs["avg_min_score_losers"] is None
    assert kwargs["avg_min_score_winners"] == pytest.approx(60.0)


def test_promotion_check_swallows_db_errors_silently():
    """A failed promotion check must never bubble up — the post-paper-
    exit hook is fire-and-forget and breaking it would block the trade
    notification path."""
    coord = _make_coordinator()

    class _BoomPool:
        def connection(self):
            class _Ctx:
                async def __aenter__(self_inner):
                    raise RuntimeError("simulated DB outage")

                async def __aexit__(self_inner, *a):
                    return False

            return _Ctx()

    coord._pool = _BoomPool()
    fake_notifier = MagicMock()
    fake_notifier.exit_intelligence_promotion_ready = AsyncMock()
    with patch("coordinator.get_notifier", return_value=fake_notifier):
        _arun(coord._check_exit_intelligence_promotion_threshold())

    fake_notifier.exit_intelligence_promotion_ready.assert_not_called()
    assert coord._exit_intel_promotion_sent is False


# ─── Post-exit hook gating ────────────────────────────────────────────


def test_post_paper_exit_skips_promotion_check_when_already_latched():
    """Once the alert has fired, the post-paper-exit hook must not
    re-spawn the threshold check — the latch is the only thing
    suppressing rate-limited Discord re-posts."""
    coord = _make_coordinator()
    coord._exit_intel_promotion_sent = True

    spawned: list[str] = []

    def _fake_create_task(coro):
        try:
            spawned.append(getattr(coro, "__qualname__", repr(coro)))
        finally:
            coro.close()  # avoid "coroutine was never awaited" warning
        return MagicMock()

    with patch("coordinator.asyncio.create_task", side_effect=_fake_create_task), \
         patch.object(coord, "_persist_trade", new=AsyncMock(return_value=(False, None))), \
         patch.object(coord, "_clear_paper_position", new=AsyncMock()), \
         patch.object(coord, "_persist_equity", new=AsyncMock()), \
         patch.object(coord, "_save_state", new=AsyncMock()), \
         patch.object(coord, "_save_and_label_features", new=AsyncMock()):
        trade = MagicMock()
        trade.ticker = "KXBTC-T"
        trade.direction = "long"
        trade.contracts = 1
        trade.entry_price = 50.0
        trade.exit_price = 52.0
        trade.pnl = 0.02
        trade.pnl_pct = 0.04
        trade.exit_reason = "TP"
        trade.candles_held = 1
        _arun(coord._persist_and_notify_exit(trade, "BTC", "paper"))

    assert not any("_check_exit_intelligence_promotion_threshold" in s for s in spawned)


def test_post_paper_exit_spawns_promotion_check_when_unlatched_and_in_shadow():
    coord = _make_coordinator()
    coord._exit_intel_promotion_sent = False
    coord._ml_data_ready_sent = True  # silence the sibling ML task spawn

    original_enabled = _set("exit_intelligence_enabled", True)
    original_shadow = _set("exit_intelligence_shadow_only", True)
    original_telem = _set("position_telemetry_enabled", True)

    spawned: list[str] = []

    def _fake_create_task(coro):
        try:
            spawned.append(getattr(coro, "__qualname__", repr(coro)))
        finally:
            coro.close()
        return MagicMock()

    try:
        with patch("coordinator.asyncio.create_task", side_effect=_fake_create_task), \
             patch.object(coord, "_persist_trade", new=AsyncMock(return_value=(False, None))), \
             patch.object(coord, "_clear_paper_position", new=AsyncMock()), \
             patch.object(coord, "_persist_equity", new=AsyncMock()), \
             patch.object(coord, "_save_state", new=AsyncMock()), \
             patch.object(coord, "_save_and_label_features", new=AsyncMock()):
            trade = MagicMock()
            trade.ticker = "KXBTC-T"
            trade.direction = "long"
            trade.contracts = 1
            trade.entry_price = 50.0
            trade.exit_price = 52.0
            trade.pnl = 0.02
            trade.pnl_pct = 0.04
            trade.exit_reason = "TP"
            trade.candles_held = 1
            _arun(coord._persist_and_notify_exit(trade, "BTC", "paper"))
    finally:
        _restore("exit_intelligence_enabled", original_enabled)
        _restore("exit_intelligence_shadow_only", original_shadow)
        _restore("position_telemetry_enabled", original_telem)

    assert any(
        "_check_exit_intelligence_promotion_threshold" in s for s in spawned
    ), f"promotion check task was not spawned; saw {spawned}"


def test_post_paper_exit_skips_promotion_check_when_shadow_disabled():
    """Once the operator flips ``EXIT_INTELLIGENCE_SHADOW_ONLY=false``,
    the gate is already promoted — no need to keep nagging the
    attribution channel."""
    coord = _make_coordinator()
    coord._exit_intel_promotion_sent = False
    coord._ml_data_ready_sent = True

    original_enabled = _set("exit_intelligence_enabled", True)
    original_shadow = _set("exit_intelligence_shadow_only", False)
    original_telem = _set("position_telemetry_enabled", True)

    spawned: list[str] = []

    def _fake_create_task(coro):
        try:
            spawned.append(getattr(coro, "__qualname__", repr(coro)))
        finally:
            coro.close()
        return MagicMock()

    try:
        with patch("coordinator.asyncio.create_task", side_effect=_fake_create_task), \
             patch.object(coord, "_persist_trade", new=AsyncMock(return_value=(False, None))), \
             patch.object(coord, "_clear_paper_position", new=AsyncMock()), \
             patch.object(coord, "_persist_equity", new=AsyncMock()), \
             patch.object(coord, "_save_state", new=AsyncMock()), \
             patch.object(coord, "_save_and_label_features", new=AsyncMock()):
            trade = MagicMock()
            trade.ticker = "KXBTC-T"
            trade.direction = "long"
            trade.contracts = 1
            trade.entry_price = 50.0
            trade.exit_price = 52.0
            trade.pnl = 0.02
            trade.pnl_pct = 0.04
            trade.exit_reason = "TP"
            trade.candles_held = 1
            _arun(coord._persist_and_notify_exit(trade, "BTC", "paper"))
    finally:
        _restore("exit_intelligence_enabled", original_enabled)
        _restore("exit_intelligence_shadow_only", original_shadow)
        _restore("position_telemetry_enabled", original_telem)

    assert not any(
        "_check_exit_intelligence_promotion_threshold" in s for s in spawned
    )


# ─── State persistence ────────────────────────────────────────────────


def test_save_state_persists_promotion_latch():
    coord, pool = _coordinator_with_pool(agg_row=(0, 0, 0, 0, 0, None, None))
    coord._exit_intel_promotion_sent = True
    _arun(coord._save_state())
    saved = pool.store.get("sizer_state")
    assert saved is not None
    assert saved.get("exit_intel_promotion_sent") is True


def test_restore_state_restores_promotion_latch():
    coord, pool = _coordinator_with_pool(agg_row=(0, 0, 0, 0, 0, None, None))
    pool.store["sizer_state"] = {
        "paper": {"bankroll": 100.0, "peak_bankroll": 100.0,
                  "daily_start_bankroll": 100.0, "weekly_start_bankroll": 100.0},
        "live": {"bankroll": 100.0, "peak_bankroll": 100.0,
                 "daily_start_bankroll": 100.0, "weekly_start_bankroll": 100.0},
        "trading_mode": "paper",
        "trading_paused": "off",
        "ml_data_ready_sent": False,
        "exit_intel_promotion_sent": True,
    }
    _arun(coord._restore_state())
    assert coord._exit_intel_promotion_sent is True


def test_restore_state_defaults_promotion_latch_off_for_legacy_state():
    coord, pool = _coordinator_with_pool(agg_row=(0, 0, 0, 0, 0, None, None))
    pool.store["sizer_state"] = {
        "paper": {"bankroll": 100.0, "peak_bankroll": 100.0,
                  "daily_start_bankroll": 100.0, "weekly_start_bankroll": 100.0},
        "live": {"bankroll": 100.0, "peak_bankroll": 100.0,
                 "daily_start_bankroll": 100.0, "weekly_start_bankroll": 100.0},
        "trading_mode": "paper",
        "trading_paused": "off",
        "ml_data_ready_sent": False,
    }
    _arun(coord._restore_state())
    assert coord._exit_intel_promotion_sent is False
