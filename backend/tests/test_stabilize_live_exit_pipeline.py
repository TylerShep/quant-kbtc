"""Regression tests for the live-exit stabilization plan."""
from __future__ import annotations

import json
import signal
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import settings as live_settings
from data.kalshi_ws import KalshiWebSocketClient
from data.manager import DataManager, OrderBookState


@contextmanager
def _set_settings(**overrides):
    """Temporarily override frozen settings fields.

    Usage:
      _set_settings(bot__health_score_breach_ticks=3)
    """
    stash = []
    try:
        for key, value in overrides.items():
            section, attr = key.split("__", 1)
            target = getattr(live_settings, section)
            previous = getattr(target, attr)
            object.__setattr__(target, attr, value)
            stash.append((target, attr, previous))
        yield
    finally:
        for target, attr, previous in reversed(stash):
            object.__setattr__(target, attr, previous)


def _book(bid: int, ask: int) -> OrderBookState:
    """Build an OrderBookState with one level on each side."""
    book = OrderBookState()
    book.apply_level("yes", bid, 10)
    book.apply_level("no", 100 - ask, 10)
    return book


def _make_coordinator():
    with patch("execution.live_trader.KalshiOrderClient"), \
         patch("data.fill_stream.KalshiAuth"), \
         patch("data.fill_stream.KalshiOrderClient", create=True), \
         patch("execution.position_manager.KalshiOrderClient", create=True), \
         patch("notifications.get_notifier"):
        from coordinator import Coordinator

        return Coordinator()


class _FakeCursor:
    def __init__(self, row=None):
        self._row = row

    async def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, store: dict):
        self._store = store

    async def execute(self, sql: str, params: tuple = ()):
        sql_lower = sql.lower()
        if "insert into bot_state" in sql_lower and params:
            payload = json.loads(params[0])
            self._store["sizer_state"] = payload
            return _FakeCursor()
        if "select value from bot_state" in sql_lower and "param_overrides" in sql_lower:
            return _FakeCursor(None)
        if "select value from bot_state" in sql_lower and "sizer_state" in sql_lower:
            row = self._store.get("sizer_state")
            return _FakeCursor((row,) if row is not None else None)
        if "select coalesce(sum(pnl)" in sql_lower:
            return _FakeCursor((0,))
        return _FakeCursor()


class _FakePool:
    def __init__(self):
        self.store: dict = {}

    def connection(self):
        pool = self

        class _Ctx:
            async def __aenter__(self_inner):
                return _FakeConn(pool.store)

            async def __aexit__(self_inner, *args):
                return False

        return _Ctx()


def test_market_state_tracks_per_ticker_books():
    dm = DataManager()
    state = dm.states["BTC"]
    state.kalshi_ticker = "KXBTC-ACTIVE"

    dm._apply_orderbook(  # type: ignore[attr-defined]
        "BTC",
        state,
        "orderbook_snapshot",
        {"market_ticker": "KXBTC-POS", "yes": [[20, 5]], "no": [[70, 7]]},
    )
    dm._apply_orderbook(  # type: ignore[attr-defined]
        "BTC",
        state,
        "orderbook_snapshot",
        {"market_ticker": "KXBTC-ACTIVE", "yes": [[60, 9]], "no": [[30, 11]]},
    )

    assert "KXBTC-POS" in state.order_books
    assert "KXBTC-ACTIVE" in state.order_books
    assert state.order_books["KXBTC-POS"].mid == pytest.approx(25.0)
    assert state.order_books["KXBTC-ACTIVE"].mid == pytest.approx(65.0)
    assert state.order_book.mid == pytest.approx(65.0)


def test_exit_price_uses_position_ticker_book():
    from coordinator import Coordinator

    coord = Coordinator.__new__(Coordinator)
    state = SimpleNamespace(
        order_books={
            "KXBTC-POS": _book(20, 30),
            "KXBTC-ACTIVE": _book(60, 70),
        },
        order_book=_book(60, 70),
    )
    trader = SimpleNamespace(
        position=SimpleNamespace(ticker="KXBTC-POS", direction="long")
    )
    assert coord._get_exit_price_for(state, trader) == pytest.approx(25.0)


def test_exit_price_returns_none_when_position_book_missing():
    from coordinator import Coordinator

    coord = Coordinator.__new__(Coordinator)
    state = SimpleNamespace(
        order_books={"KXBTC-ACTIVE": _book(60, 70)},
        order_book=_book(60, 70),
    )
    trader = SimpleNamespace(
        position=SimpleNamespace(ticker="KXBTC-POS", direction="long")
    )
    assert coord._get_exit_price_for(state, trader) is None


@pytest.mark.asyncio
async def test_kalshi_subscribe_includes_watched_position_tickers():
    with patch("data.kalshi_ws.KalshiAuth", autospec=True):
        client = KalshiWebSocketClient(markets=["BTC"], on_update=lambda *_: None)
    client.active_tickers = {"BTC": "KXBTC-ACTIVE"}
    client.watched_position_tickers = {"KXBTC-POS": "BTC"}

    ws = MagicMock()
    ws.send = AsyncMock()
    await client._subscribe(ws)

    first_payload = json.loads(ws.send.call_args_list[0].args[0])
    tickers = set(first_payload["params"]["market_tickers"])
    assert tickers == {"KXBTC-ACTIVE", "KXBTC-POS"}


def test_register_position_ticker_triggers_resubscribe_when_ws_open():
    from coordinator import Coordinator

    coord = Coordinator.__new__(Coordinator)
    ws_client = SimpleNamespace(
        watched_position_tickers={},
        _ws_is_open=lambda: True,
        refresh_position_subscriptions=AsyncMock(),
    )
    coord.data_manager = SimpleNamespace(_kalshi_ws=ws_client)

    created = []

    def _fake_create_task(coro):
        created.append(coro)
        return MagicMock()

    with patch("coordinator.asyncio.create_task", side_effect=_fake_create_task):
        coord._register_position_ticker("KXBTC-POS", "BTC")

    assert ws_client.watched_position_tickers["KXBTC-POS"] == "BTC"
    assert len(created) == 1
    for coro in created:
        coro.close()


def test_phase3_defaults_set_safe_exit_hardening(monkeypatch):
    from config.settings import RiskConfig

    monkeypatch.delenv("HARD_STOP_LOSS_PCT", raising=False)
    monkeypatch.delenv("MIN_CANDLES_BEFORE_EARLY_EXIT", raising=False)
    cfg = RiskConfig()
    assert cfg.hard_stop_loss_pct == pytest.approx(0.10)
    assert cfg.min_candles_before_early_exit == 1


def test_hard_stop_loss_bypasses_warmup_gate():
    from coordinator import Coordinator

    coord = Coordinator.__new__(Coordinator)
    coord.candle_aggregator = SimpleNamespace(recent=lambda n: [])
    coord._health_breach_counts = {}
    coord._last_health_snapshot = {"paper": None, "live": None}
    coord._last_position_telemetry_ts = {}
    coord._pool = None
    coord._maybe_persist_position_telemetry = lambda **kwargs: None

    state = SimpleNamespace(
        order_books={"KXBTC-POS": _book(20, 30)},
        order_book=_book(60, 70),
        time_remaining_sec=600,
        spot_price=81000.0,
    )
    features = SimpleNamespace(obi=0.5, spot_roc_30s=None, spot_roc_60s=None)
    position = SimpleNamespace(
        ticker="KXBTC-POS",
        direction="long",
        entry_price=40.0,
        contracts=1,
        candles_held=0,
        max_favorable_excursion=0.0,
        max_adverse_excursion=0.0,
        entry_roc=0.2,
        regime_at_entry="MEDIUM",
        position_uid="live-pos-1",
    )
    trader = SimpleNamespace(position=position)

    with _set_settings(
        bot__exit_intelligence_enabled=False,
        risk__hard_stop_loss_pct=0.10,
        risk__min_candles_before_early_exit=2,
    ):
        reason = coord._check_exits_for(
            state=state,
            features=features,
            regime="MEDIUM",
            trader=trader,
            mode="live",
        )
    assert reason == "HARD_STOP_LOSS"


@pytest.mark.asyncio
async def test_rss_watchdog_persists_state_before_sigterm():
    from coordinator import Coordinator

    coord = Coordinator.__new__(Coordinator)
    coord._resolve_watchdog_memory_limit_bytes = MagicMock(return_value=1000)
    coord._last_watchdog_tracemalloc_ts = 0.0
    coord._bg_persist_tasks = set()
    coord._bg_persist_dropped = 0
    coord.data_manager = SimpleNamespace(states={})
    coord._save_state = AsyncMock()
    coord._save_paper_position = AsyncMock()
    coord._rss_watchdog_log_tracemalloc = AsyncMock()
    coord.live_trader = SimpleNamespace(
        position_manager=SimpleNamespace(_persist_state=AsyncMock())
    )

    process = SimpleNamespace(memory_info=MagicMock(return_value=SimpleNamespace(rss=900)))

    with _set_settings(
        bot__rss_watchdog_poll_sec=30.0,
        bot__rss_watchdog_threshold_pct=0.85,
        bot__rss_watchdog_tracemalloc_interval_sec=300.0,
    ), patch("coordinator.psutil.Process", return_value=process), \
         patch("coordinator.os.kill") as kill_mock:
        await coord._rss_watchdog_loop()

    coord._save_state.assert_awaited_once()
    coord.live_trader.position_manager._persist_state.assert_awaited_once()
    coord._save_paper_position.assert_awaited_once()
    kill_mock.assert_called_once()
    assert kill_mock.call_args.args[1] == signal.SIGTERM


@pytest.mark.asyncio
async def test_save_restore_state_round_trips_health_breach_counts():
    coord = _make_coordinator()
    pool = _FakePool()
    coord._pool = pool
    coord._health_breach_counts = {"live:abc": 3, "paper:def": 1}

    await coord._save_state()

    restored = _make_coordinator()
    restored._pool = pool
    restored.sync_live_bankroll = AsyncMock()
    restored._cancel_stale_orders = AsyncMock()
    restored._reconcile_live_positions = AsyncMock()

    await restored._restore_state()
    assert restored._health_breach_counts == {"live:abc": 3, "paper:def": 1}


@pytest.mark.asyncio
async def test_candle_close_triggers_live_position_persist():
    coord = _make_coordinator()
    coord._tick_count = 1
    coord.trading_mode = "live"
    coord._run_settlement_guards = MagicMock()
    coord._run_paper_lane = MagicMock()
    coord._run_live_lane = MagicMock()
    coord._pool = object()
    coord._spawn_bg_persist = MagicMock()

    completed_candle = SimpleNamespace(
        high=1.0, low=1.0, close=1.0, open=1.0, timestamp=1.0, volume=0.0
    )
    coord.feature_engine.update = MagicMock(
        return_value=SimpleNamespace(
            obi=0.5,
            to_dict=lambda: {},
            total_bid_vol=1,
            total_ask_vol=1,
            spread_cents=1,
        )
    )
    coord.candle_aggregator.on_tick = MagicMock(return_value=completed_candle)
    coord.spread_filter.update = MagicMock()
    coord.atr_filter.update = MagicMock()
    coord.atr_filter.current_regime = "MEDIUM"
    coord.paper_trader.position = None
    coord.live_trader.position = SimpleNamespace(candles_held=0)
    coord.live_trader.position_manager._persist_state = AsyncMock()

    state = SimpleNamespace(
        symbol="BTC",
        spot_price=100.0,
        kalshi_ticker="KXBTC-ACTIVE",
        order_book=_book(40, 60),
        time_remaining_sec=600,
        volume=0.0,
    )

    coord._on_market_update("BTC", state)

    assert coord.live_trader.position.candles_held == 1
    assert coord.live_trader.position_manager._persist_state.call_count == 1
    scheduled = [c.args[0] for c in coord._spawn_bg_persist.call_args_list]
    for coro in scheduled:
        if hasattr(coro, "close"):
            coro.close()
