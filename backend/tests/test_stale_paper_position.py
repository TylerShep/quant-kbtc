"""Tests for BUG-035: stale paper position cleanup and ticker close-time parsing.

The 2026-05-05 incident saw a paper position on ``KXBTC-26MAY0515-B81350``
(a contract that closed at 18:15 UTC) survive a container restart 2.5h
*after* its contract had settled. The position then sat unmanaged for
another 5h while the EXPIRY_GUARD branch of ``_run_settlement_guards``
spammed ``skip_no_liquidity`` ~23 times per second every time a different
(live) contract entered its own pre-close window. CPU pinned to 100%,
websocket keepalives broke every 3-5 minutes, the DB pool starved, and
paper trading halted entirely (62 paper trades in the prior 36h, then 0
for 7.5h).

These tests pin three new behaviours:
1. ``_ticker_close_time`` parses the Kalshi ticker format reliably and
   returns ``None`` for unparseable inputs (so callers fall back safely).
2. ``_restore_paper_position`` drops a persisted row whose ticker has a
   close_time in the past.
3. ``_run_settlement_guards`` synthesises a STALE_TICKER_CLEANUP exit when
   a paper position has outlived its contract by more than the configured
   grace window.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from execution.paper_trader import PaperPosition


def _arun(coro):
    """Run a coroutine on a private loop without closing the global default."""
    loop = asyncio.new_event_loop()
    prev = None
    try:
        try:
            prev = asyncio.get_event_loop()
        except RuntimeError:
            prev = None
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        if prev is not None and not prev.is_closed():
            asyncio.set_event_loop(prev)
        else:
            asyncio.set_event_loop(asyncio.new_event_loop())


def _make_coordinator():
    """Construct a Coordinator with the network-touching deps stubbed."""
    with patch("execution.live_trader.KalshiOrderClient"), \
         patch("data.fill_stream.KalshiAuth"), \
         patch("data.fill_stream.KalshiOrderClient", create=True), \
         patch("execution.position_manager.KalshiOrderClient", create=True), \
         patch("notifications.get_notifier"):
        from coordinator import Coordinator
        return Coordinator()


# ── Section 1: ticker close-time parsing ───────────────────────────────────

class TestTickerCloseTime:
    def test_parses_standard_btc_ticker_during_edt(self):
        """The ticker hour is Eastern Time. May 5 is in EDT (UTC-4), so
        a ticker tagged ``-15`` (= 15:00 ET) returns 19:00 UTC."""
        from coordinator import _ticker_close_time
        result = _ticker_close_time("KXBTC-26MAY0515-B81350")
        assert result == datetime(2026, 5, 5, 19, 0, 0, tzinfo=timezone.utc)

    def test_parses_ticker_with_t_strike_format(self):
        """April 20 is in EDT (UTC-4), so 18:00 ET = 22:00 UTC."""
        from coordinator import _ticker_close_time
        result = _ticker_close_time("KXBTC-26APR2018-T75500")
        assert result == datetime(2026, 4, 20, 22, 0, 0, tzinfo=timezone.utc)

    def test_parses_eth_ticker(self):
        """ETH ticker, May 5 EDT, 09:00 ET = 13:00 UTC."""
        from coordinator import _ticker_close_time
        result = _ticker_close_time("KXETH-26MAY0509-B3500")
        assert result == datetime(2026, 5, 5, 13, 0, 0, tzinfo=timezone.utc)

    def test_parses_ticker_during_est_winter(self):
        """January is in EST (UTC-5), so a ticker tagged ``-15`` (= 15:00 ET)
        returns 20:00 UTC, not 19:00 UTC. This pin guarantees DST handling
        is automatic and we don't regress to a fixed UTC-4 offset."""
        from coordinator import _ticker_close_time
        result = _ticker_close_time("KXBTC-26JAN1515-B81350")
        assert result == datetime(2026, 1, 15, 20, 0, 0, tzinfo=timezone.utc)

    def test_parses_ticker_matches_kalshi_api_close_time(self):
        """Production fingerprint from 2026-05-06: the active resolver
        returned ``KXBTC-26MAY0610-B81950`` and the Kalshi API said its
        ``close_time`` was ``2026-05-06T14:00:00Z``. The parser MUST
        agree, otherwise the entry guard rejects the live ticker for
        the entire 4-5 hour window per session."""
        from coordinator import _ticker_close_time
        result = _ticker_close_time("KXBTC-26MAY0610-B81950")
        assert result == datetime(2026, 5, 6, 14, 0, 0, tzinfo=timezone.utc)

    def test_returns_none_for_empty_string(self):
        from coordinator import _ticker_close_time
        assert _ticker_close_time("") is None

    def test_returns_none_for_unparseable_ticker(self):
        from coordinator import _ticker_close_time
        assert _ticker_close_time("not-a-ticker") is None

    def test_returns_none_for_invalid_month_abbrev(self):
        from coordinator import _ticker_close_time
        # XYZ is not a valid month
        assert _ticker_close_time("KXBTC-26XYZ0515-B81350") is None

    def test_returns_none_for_invalid_date(self):
        from coordinator import _ticker_close_time
        # February 30 doesn't exist
        assert _ticker_close_time("KXBTC-26FEB3015-B81350") is None

    def test_returns_none_for_missing_strike(self):
        from coordinator import _ticker_close_time
        assert _ticker_close_time("KXBTC-26MAY0515-") is None


# ── Section 2: _restore_paper_position drops stale rows ────────────────────


class _Cursor:
    def __init__(self, row=None):
        self._row = row

    async def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, store: dict):
        self.store = store
        self.executed: list = []

    async def execute(self, sql: str, params: tuple = ()) -> _Cursor:
        self.executed.append((sql, params))
        sql_lower = sql.lower()
        if "select value from bot_state" in sql_lower and "paper_open_position" in sql_lower:
            row = self.store.get("paper_open_position")
            return _Cursor((row,) if row is not None else None)
        if "delete from bot_state" in sql_lower and "paper_open_position" in sql_lower:
            self.store.pop("paper_open_position", None)
        return _Cursor()


class _Pool:
    def __init__(self):
        self.store: dict = {}

    def connection(self):
        store = self.store

        class _Ctx:
            async def __aenter__(self_inner):
                return _Conn(store)

            async def __aexit__(self_inner, *a):
                return False

        return _Ctx()


class TestRestorePaperPositionStalenessCheck:
    def _build_payload(self, ticker: str, hours_ago: float = 0.5) -> dict:
        entry = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        return {
            "ticker": ticker,
            "direction": "long",
            "contracts": 100,
            "entry_price": 25.0,
            "entry_time": entry.isoformat(),
            "conviction": "NORMAL",
            "regime_at_entry": "MEDIUM",
            "entry_obi": 0.6,
            "entry_roc": 0.0,
            "candles_held": 0,
            "max_favorable_excursion": 0.0,
            "max_adverse_excursion": 0.0,
            "signal_driver": "OBI",
            "position_uid": "paper-test-uid",
        }

    def test_drops_position_for_contract_that_closed_yesterday(self):
        """The exact 2026-05-05 incident fingerprint."""
        coord = _make_coordinator()
        pool = _Pool()
        coord._pool = pool

        # Pick a contract close hour that is definitively in the past.
        past_close = datetime.now(timezone.utc) - timedelta(hours=24)
        # Build a ticker for the day before today at hour 0 (rounded to the
        # day - so always > 1h in the past regardless of when test runs).
        yy = past_close.strftime("%y")
        mmm = past_close.strftime("%b").upper()
        dd = past_close.strftime("%d")
        ticker = f"KXBTC-{yy}{mmm}{dd}00-B81350"
        payload = self._build_payload(ticker)
        pool.store["paper_open_position"] = payload

        _arun(coord._restore_paper_position())

        # Position should NOT have been restored, and the row should be cleared.
        assert coord.paper_trader.position is None
        assert "paper_open_position" not in pool.store

    def test_restores_position_for_currently_active_contract(self):
        """Don't break the legitimate restore path. Build an ET-encoded
        ticker far enough in the future that even after ET->UTC conversion
        it's clearly not stale."""
        from coordinator import _ET_TZ
        coord = _make_coordinator()
        pool = _Pool()
        coord._pool = pool

        # +24h in ET, then format the components in ET. This way the
        # resulting UTC close_time after the parser converts it is also
        # +24h regardless of EST/EDT.
        et_now = datetime.now(timezone.utc).astimezone(_ET_TZ)
        future_et = et_now + timedelta(hours=24)
        yy = future_et.strftime("%y")
        mmm = future_et.strftime("%b").upper()
        dd = future_et.strftime("%d")
        hh = future_et.strftime("%H")
        ticker = f"KXBTC-{yy}{mmm}{dd}{hh}-B81350"
        payload = self._build_payload(ticker)
        pool.store["paper_open_position"] = payload

        _arun(coord._restore_paper_position())

        assert coord.paper_trader.position is not None
        assert coord.paper_trader.position.ticker == ticker
        assert pool.store.get("paper_open_position") is not None

    def test_restores_position_when_ticker_unparseable(self):
        """Fail-open: unparseable ticker means we don't *know* it's stale, so
        restore as before. The watchdog in _run_settlement_guards is the
        backstop for genuinely-dead positions."""
        coord = _make_coordinator()
        pool = _Pool()
        coord._pool = pool

        payload = self._build_payload("WEIRD-CUSTOM-TICKER")
        pool.store["paper_open_position"] = payload

        _arun(coord._restore_paper_position())

        # We can't tell if it's stale, so we restore.
        assert coord.paper_trader.position is not None


# ── Section 3: settlement-guards watchdog forces synthetic exit ────────────


def _make_market_state(symbol: str, kalshi_ticker: str, time_remaining_sec: float | None):
    """Build a minimal MarketState-shaped object for the settlement guard."""
    return SimpleNamespace(
        symbol=symbol,
        kalshi_ticker=kalshi_ticker,
        time_remaining_sec=time_remaining_sec,
        resolved=False,
        resolved_outcome=None,
        order_book=SimpleNamespace(),
        spot_price=80000.0,
    )


class TestStalePaperPositionWatchdog:
    def _give_paper_position(self, coord, ticker: str, entry_age_sec: float = 60.0):
        coord.paper_trader.position = PaperPosition(
            ticker=ticker,
            direction="long",
            contracts=50,
            entry_price=20.0,
            entry_time=datetime.now(timezone.utc) - timedelta(seconds=entry_age_sec),
            conviction="NORMAL",
            regime_at_entry="MEDIUM",
            entry_obi=0.6,
            entry_roc=0.0,
            candles_held=0,
            signal_driver="OBI",
            position_uid="paper-test-uid",
        )

    def test_watchdog_force_closes_position_when_contract_outlived(self):
        from unittest.mock import MagicMock
        coord = _make_coordinator()
        # Stub the post-exit callback so we don't need a running event loop
        # (it would otherwise call asyncio.create_task for persist + notify).
        coord._on_trade_exit = MagicMock(side_effect=lambda *a, **kw: None)

        # Pick a stale ticker (yesterday at midnight)
        stale_close = datetime.now(timezone.utc) - timedelta(hours=24)
        yy = stale_close.strftime("%y")
        mmm = stale_close.strftime("%b").upper()
        dd = stale_close.strftime("%d")
        stale_ticker = f"KXBTC-{yy}{mmm}{dd}00-B81350"
        self._give_paper_position(coord, stale_ticker)

        # Active state has rotated to a different (current) ticker
        state = _make_market_state(
            symbol="BTC",
            kalshi_ticker="KXBTC-99DEC3123-B99999",
            time_remaining_sec=1000.0,
        )

        # Run the guard
        coord._run_settlement_guards("BTC", state, coord.paper_trader, "paper")

        # Position should be closed via STALE_TICKER_CLEANUP
        assert coord.paper_trader.position is None
        assert len(coord.paper_trader.trades) == 1
        trade = coord.paper_trader.trades[0]
        assert trade.exit_reason == "STALE_TICKER_CLEANUP"
        assert trade.ticker == stale_ticker
        # Exit at entry price (zero gross PnL; only fees deduct)
        assert trade.exit_price == 20.0
        assert trade.pnl < 0  # fees only
        assert abs(trade.pnl) < 5.0  # Fees should be small
        # The post-exit callback was called once with the synthetic trade
        coord._on_trade_exit.assert_called_once()

    def test_watchdog_does_not_fire_for_active_contract(self):
        from coordinator import _ET_TZ
        coord = _make_coordinator()
        # Future close time -> not stale (use ET-encoded ticker so the
        # parser converts back to a UTC value that is genuinely in the
        # future, regardless of EST/EDT).
        et_now = datetime.now(timezone.utc).astimezone(_ET_TZ)
        future_et = et_now + timedelta(hours=24)
        yy = future_et.strftime("%y")
        mmm = future_et.strftime("%b").upper()
        dd = future_et.strftime("%d")
        hh = future_et.strftime("%H")
        active_ticker = f"KXBTC-{yy}{mmm}{dd}{hh}-B81350"
        self._give_paper_position(coord, active_ticker)

        state = _make_market_state(
            symbol="BTC",
            kalshi_ticker=active_ticker,
            time_remaining_sec=1000.0,
        )

        coord._run_settlement_guards("BTC", state, coord.paper_trader, "paper")

        # Position should still be open
        assert coord.paper_trader.position is not None
        assert coord.paper_trader.position.ticker == active_ticker

    def test_watchdog_respects_grace_window(self):
        """A position whose contract closed seconds ago should not yet
        be force-closed -- the normal lifecycle_settled / settlement-guard
        path needs a chance to land first."""
        from coordinator import _ET_TZ
        coord = _make_coordinator()
        # Build a ticker whose close time is in the very recent past (in
        # ET), then inflate the grace window to a value that always
        # exceeds the elapsed time since the most recent hour close.
        et_now = datetime.now(timezone.utc).astimezone(_ET_TZ)
        yy = et_now.strftime("%y")
        mmm = et_now.strftime("%b").upper()
        dd = et_now.strftime("%d")
        hh = et_now.strftime("%H")
        ticker = f"KXBTC-{yy}{mmm}{dd}{hh}-B81350"
        self._give_paper_position(coord, ticker)

        # BotConfig is a frozen dataclass; bypass with object.__setattr__.
        from config import settings
        original = settings.bot.stale_paper_grace_sec
        object.__setattr__(settings.bot, "stale_paper_grace_sec", 99999)
        try:
            state = _make_market_state(
                symbol="BTC",
                kalshi_ticker="KXBTC-99DEC3123-B99999",
                time_remaining_sec=1000.0,
            )
            coord._run_settlement_guards("BTC", state, coord.paper_trader, "paper")
            # Position should still be open (we're inside the grace window)
            assert coord.paper_trader.position is not None
        finally:
            object.__setattr__(settings.bot, "stale_paper_grace_sec", original)

    def test_evaluate_entry_for_skips_stale_active_ticker(self):
        """BUG-035 entry-side hardening: refuse to enter on a ticker whose
        ticker-encoded close_time is in the past, even if the bot's
        active-ticker resolver claims it's the live one."""
        from unittest.mock import MagicMock
        coord = _make_coordinator()
        # Pick a ticker whose close_time is clearly in the past
        stale_close = datetime.now(timezone.utc) - timedelta(hours=24)
        yy = stale_close.strftime("%y")
        mmm = stale_close.strftime("%b").upper()
        dd = stale_close.strftime("%d")
        stale_ticker = f"KXBTC-{yy}{mmm}{dd}00-B81350"

        state = _make_market_state(
            symbol="BTC",
            kalshi_ticker=stale_ticker,
            time_remaining_sec=1000.0,
        )
        # Stub all the things _evaluate_entry_for would otherwise touch
        # past the early-return guard. If the guard fails to fire, these
        # mocks would be invoked and the test would fail explicitly.
        coord.feature_engine.obi_history = MagicMock(return_value=[])
        coord.candle_aggregator.get_candles = MagicMock(return_value=[])
        coord.paper_breaker.can_trade = MagicMock(return_value=(True, None))
        features = SimpleNamespace(
            total_bid_vol=100.0, total_ask_vol=100.0, obi=0.0,
        )

        coord._evaluate_entry_for(
            "BTC", state, features, "MEDIUM",
            coord.paper_trader, coord.paper_sizer, coord.paper_breaker, "paper",
        )

        # The early-return guard should have prevented the breaker check
        # (and any downstream signal evaluation) from running.
        assert coord.paper_trader.position is None
        coord.paper_breaker.can_trade.assert_not_called()

    def test_evaluate_entry_for_proceeds_when_active_ticker_is_live(self):
        """The entry-side staleness guard must not interfere with normal
        entry evaluation on a healthy active ticker. We assert by proving
        the breaker check (the very next step after the staleness gate)
        runs to completion."""
        from unittest.mock import MagicMock
        from coordinator import _ET_TZ
        coord = _make_coordinator()
        # Ticker +24h in ET = clearly future even after ET->UTC conversion.
        et_now = datetime.now(timezone.utc).astimezone(_ET_TZ)
        future_et = et_now + timedelta(hours=24)
        yy = future_et.strftime("%y")
        mmm = future_et.strftime("%b").upper()
        dd = future_et.strftime("%d")
        hh = future_et.strftime("%H")
        active_ticker = f"KXBTC-{yy}{mmm}{dd}{hh}-B81350"

        state = _make_market_state(
            symbol="BTC",
            kalshi_ticker=active_ticker,
            time_remaining_sec=1000.0,
        )
        # Make the breaker check raise so we know execution got *past*
        # the staleness gate (sentinel exception → caught by the
        # assertRaises context to keep the test self-contained).
        sentinel = RuntimeError("breaker_called")
        coord.paper_breaker.can_trade = MagicMock(side_effect=sentinel)
        features = SimpleNamespace(
            total_bid_vol=100.0, total_ask_vol=100.0, obi=0.0,
        )

        with pytest.raises(RuntimeError, match="breaker_called"):
            coord._evaluate_entry_for(
                "BTC", state, features, "MEDIUM",
                coord.paper_trader, coord.paper_sizer, coord.paper_breaker, "paper",
            )

        # If the staleness gate had bailed, the breaker would never
        # have been called and this assertion would fail.
        coord.paper_breaker.can_trade.assert_called_once()

    def test_watchdog_does_not_fire_for_live_lane(self):
        """Live lane has its own orphan/reconciliation paths; the watchdog
        is paper-only by design so we don't double-handle live positions."""
        coord = _make_coordinator()
        stale_close = datetime.now(timezone.utc) - timedelta(hours=24)
        yy = stale_close.strftime("%y")
        mmm = stale_close.strftime("%b").upper()
        dd = stale_close.strftime("%d")
        stale_ticker = f"KXBTC-{yy}{mmm}{dd}00-B81350"

        # Force the live trader to look like it has a position
        coord.live_trader.position_manager.position = SimpleNamespace(
            ticker=stale_ticker,
            direction="long",
            contracts=5,
            entry_price=20.0,
        )
        # Stub `has_position` since we only need the guard to find it
        # (the live branch never actually mutates anything in this test).
        try:
            coord.live_trader.position_manager.state = (
                coord.live_trader.position_manager.state
            )
        except AttributeError:
            pass

        state = _make_market_state(
            symbol="BTC",
            kalshi_ticker="KXBTC-99DEC3123-B99999",
            time_remaining_sec=1000.0,
        )

        # Run the guard for the LIVE lane
        coord._run_settlement_guards("BTC", state, coord.live_trader, "live")

        # No paper trade should have been created (we ran the live lane)
        assert len(coord.paper_trader.trades) == 0


class TestHealthExitHysteresis:
    def test_confirmation_blocks_exit_without_deterioration(self):
        coord = _make_coordinator()
        pos = SimpleNamespace(
            direction="long",
            entry_obi=0.70,
            entry_roc=0.20,
        )
        from config import settings

        original_enabled = settings.bot.health_exit_confirmation_enabled
        original_roc_delta = settings.bot.health_exit_confirmation_roc_delta
        original_obi_delta = settings.bot.health_exit_confirmation_obi_delta
        original_neutral = settings.bot.health_exit_confirmation_neutral_obi
        object.__setattr__(settings.bot, "health_exit_confirmation_enabled", True)
        object.__setattr__(settings.bot, "health_exit_confirmation_roc_delta", 0.05)
        object.__setattr__(settings.bot, "health_exit_confirmation_obi_delta", 0.05)
        object.__setattr__(settings.bot, "health_exit_confirmation_neutral_obi", 0.50)
        try:
            confirmed = coord._health_exit_confirmation_met(
                pos=pos,
                current_obi=0.69,
                current_roc=0.18,
            )
        finally:
            object.__setattr__(settings.bot, "health_exit_confirmation_enabled", original_enabled)
            object.__setattr__(settings.bot, "health_exit_confirmation_roc_delta", original_roc_delta)
            object.__setattr__(settings.bot, "health_exit_confirmation_obi_delta", original_obi_delta)
            object.__setattr__(settings.bot, "health_exit_confirmation_neutral_obi", original_neutral)

        assert confirmed is False

    def test_confirmation_allows_exit_on_clear_deterioration(self):
        coord = _make_coordinator()
        pos = SimpleNamespace(
            direction="short",
            entry_obi=0.30,
            entry_roc=-0.20,
        )
        from config import settings

        original_enabled = settings.bot.health_exit_confirmation_enabled
        original_roc_delta = settings.bot.health_exit_confirmation_roc_delta
        original_obi_delta = settings.bot.health_exit_confirmation_obi_delta
        original_neutral = settings.bot.health_exit_confirmation_neutral_obi
        object.__setattr__(settings.bot, "health_exit_confirmation_enabled", True)
        object.__setattr__(settings.bot, "health_exit_confirmation_roc_delta", 0.05)
        object.__setattr__(settings.bot, "health_exit_confirmation_obi_delta", 0.05)
        object.__setattr__(settings.bot, "health_exit_confirmation_neutral_obi", 0.50)
        try:
            confirmed = coord._health_exit_confirmation_met(
                pos=pos,
                current_obi=0.55,
                current_roc=-0.05,
            )
        finally:
            object.__setattr__(settings.bot, "health_exit_confirmation_enabled", original_enabled)
            object.__setattr__(settings.bot, "health_exit_confirmation_roc_delta", original_roc_delta)
            object.__setattr__(settings.bot, "health_exit_confirmation_obi_delta", original_obi_delta)
            object.__setattr__(settings.bot, "health_exit_confirmation_neutral_obi", original_neutral)

        assert confirmed is True
