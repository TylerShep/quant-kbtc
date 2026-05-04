"""Regression tests for BUG-028: EXPIRY_409 race fixes.

Three layers of defense are exercised:

  Layer 1 (coordinator pre-evaluation guard):
    ``Coordinator._is_near_expiry`` must treat ``time_remaining_sec is
    None`` as ``near_expiry=True``. Every observed EXPIRY_409_SETTLED
    trade prior to this fix had ``state.expiry_time = None`` at entry
    time -- the new contract's ``ticker`` WS event had not yet
    populated ``expiry_time`` when ``_resolve_tickers`` rotated
    ``state.kalshi_ticker``. The previous guard
    (``time_remaining_sec is not None and ... < 120``) silently
    passed, sending an entry order against a contract whose actual
    close time was unknown to the bot.

  Layer 2 (position_manager pre-place market-status check):
    Right before ``client.create_order``, the position manager must
    fetch the market and abort if Kalshi reports ``status != 'open'``.
    Catches the residual ~10-100ms race between "decide to enter" and
    "order hits the wire" -- the case where Layer 1's snapshot of
    time_remaining_sec was good but Kalshi closed the book in the
    intervening millis.

  Layer 3 (telemetry):
    ``Coordinator._log_near_expiry_skip`` increments a per-mode
    counter that is surfaced on ``/api/diagnostics`` so the operator
    can see how often the guard is firing without grepping logs.

Companion entries: BUG-022 (phantom-entry cooldown), BUG-027 (PnL
formula). The test file mirrors the structure of test_phantom_entry.py
to make the BUG-class regression coverage easy to read together.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from execution.position_manager import (
    ENTRY_CANCEL_SETTLE_SEC,
    ENTRY_REST_BAILOUT_SEC,
    FILL_POLL_INTERVAL,
    PositionManager,
    PositionState,
)


# ── Speed-up fixture ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fast_clocks():
    """Shrink real-time waits inside enter() so each test runs in <100ms."""
    async def _instant_sleep(_seconds: float) -> None:
        return None

    with patch("execution.position_manager.ENTRY_REST_BAILOUT_SEC", 0.0), \
         patch("execution.position_manager.ENTRY_CANCEL_SETTLE_SEC", 0.0), \
         patch("execution.position_manager.FILL_POLL_INTERVAL", 0.001), \
         patch("execution.position_manager.FILL_POLL_TIMEOUT", 0.05), \
         patch("execution.position_manager.asyncio.sleep", _instant_sleep):
        yield


# ── Helpers ───────────────────────────────────────────────────────────


def _make_pm() -> PositionManager:
    client = MagicMock()
    return PositionManager(client)


def _order_resp(order_id: str = "ord-1") -> dict:
    return {"order": {"order_id": order_id}}


def _positions_resp(ticker: str, position_fp: float) -> dict:
    return {
        "market_positions": [
            {"ticker": ticker, "position_fp": str(position_fp),
             "total_traded_dollars": "0"}
        ]
    }


def _get_order_resp(*, status: str = "executed", filled: int = 5,
                    yes_price_dollars: str = "0.20",
                    taker_fill_cost_dollars: str = "1.00",
                    taker_fees_dollars: str = "0.10") -> dict:
    return {"order": {
        "status": status,
        "fill_count_fp": str(filled),
        "yes_price_dollars": yes_price_dollars,
        "taker_fill_cost_dollars": taker_fill_cost_dollars,
        "taker_fees_dollars": taker_fees_dollars,
    }}


# ══════════════════════════════════════════════════════════════════════
# Layer 1: Coordinator._is_near_expiry
# ══════════════════════════════════════════════════════════════════════


def _import_coordinator_module():
    """Import the coordinator module without instantiating Coordinator.

    Importing ``backend.coordinator`` triggers a chain of side-effect
    imports (ml.inference, data.manager) that pull in numpy, websockets,
    and other heavy deps that the production runtime always has but a
    minimal test venv may not. We don't actually need any of those for
    the BUG-028 unit tests -- we only need the bound ``_is_near_expiry``
    and ``_log_near_expiry_skip`` methods. So we stub out the heavy
    imports just for the import, then restore the original modules so
    we don't poison ``sys.modules`` for tests that run after us (e.g.
    pytest's own ``_pytest.python_api`` calls ``numpy.isscalar`` on
    arbitrary asserted values; an empty stub there crashes them).
    """
    import sys
    import types

    deps = ("numpy", "websockets")
    saved: dict = {}
    installed_stubs: list = []
    for dep in deps:
        if dep in sys.modules:
            continue
        try:
            __import__(dep)
        except ModuleNotFoundError:
            saved[dep] = sys.modules.get(dep)  # None
            sys.modules[dep] = types.ModuleType(dep)
            installed_stubs.append(dep)

    if "coordinator" in sys.modules:
        del sys.modules["coordinator"]
    try:
        return __import__("coordinator")
    finally:
        for dep in installed_stubs:
            if saved.get(dep) is None:
                sys.modules.pop(dep, None)
            else:
                sys.modules[dep] = saved[dep]


class TestCoordinatorNearExpiryGuard:
    """The fix for BUG-028 lives almost entirely in this branch.

    Before the fix, ``near_expiry = state.time_remaining_sec is not None
    and state.time_remaining_sec < 120``. When ``time_remaining_sec`` was
    ``None`` (the actual production state every time EXPIRY_409 fired),
    ``near_expiry`` evaluated to False and the entry pipeline ran.

    Tests target the bound method directly via ``__new__`` so we don't
    pay the full Coordinator init cost (DB pools, WS clients, feature
    engines) for what is fundamentally a 4-line boolean.
    """

    def _make_coord(self):
        coord_mod = _import_coordinator_module()
        return coord_mod.Coordinator.__new__(coord_mod.Coordinator)

    def test_none_remaining_returns_true(self):
        """The actual BUG-028 root cause. None must be treated as
        too-close-to-expiry; the bot has no proof a fresh ticker has
        time to round-trip in.
        """
        coord = self._make_coord()
        with patch("coordinator.settings") as s:
            s.bot.min_seconds_to_expiry = 120
            assert coord._is_near_expiry(None) is True

    def test_below_threshold_returns_true(self):
        coord = self._make_coord()
        with patch("coordinator.settings") as s:
            s.bot.min_seconds_to_expiry = 120
            for sec in (0, 1, 30, 60, 119):
                assert coord._is_near_expiry(sec) is True, f"failed at {sec}s"

    def test_above_threshold_returns_false(self):
        coord = self._make_coord()
        with patch("coordinator.settings") as s:
            s.bot.min_seconds_to_expiry = 120
            for sec in (120, 121, 200, 600, 14 * 60):
                assert coord._is_near_expiry(sec) is False, f"failed at {sec}s"

    def test_threshold_is_strict_less_than(self):
        """At threshold exactly we permit entry; only strictly below it
        do we block. This matches the original 120s semantic so the
        BUG-028 fix only narrows the guard for the None case rather
        than tightening it for everyone.
        """
        coord = self._make_coord()
        with patch("coordinator.settings") as s:
            s.bot.min_seconds_to_expiry = 120
            assert coord._is_near_expiry(120) is False
            assert coord._is_near_expiry(119) is True

    def test_threshold_is_configurable_via_settings(self):
        """The MIN_SECONDS_TO_EXPIRY env knob must be honored. Not a
        BUG-028 regression per se, but it's the operator's only knob
        for tightening or loosening the gate without a code change.
        """
        coord = self._make_coord()
        with patch("coordinator.settings") as s:
            s.bot.min_seconds_to_expiry = 30
            assert coord._is_near_expiry(60) is False  # would block at 120, allows at 30
            assert coord._is_near_expiry(20) is True
        with patch("coordinator.settings") as s:
            s.bot.min_seconds_to_expiry = 300
            assert coord._is_near_expiry(60) is True  # tighter gate

    def test_negative_remaining_treated_as_near_expiry(self):
        """Defensive: if upstream ever passes a negative (clock skew,
        un-clamped subtraction), we still block rather than silently
        permit a negative-time entry.
        """
        coord = self._make_coord()
        with patch("coordinator.settings") as s:
            s.bot.min_seconds_to_expiry = 120
            assert coord._is_near_expiry(-5) is True
            assert coord._is_near_expiry(-300) is True


# ══════════════════════════════════════════════════════════════════════
# Layer 1 telemetry: Coordinator._log_near_expiry_skip
# ══════════════════════════════════════════════════════════════════════


class TestNearExpirySkipCounter:
    """Counter must increment per-mode and be visible on /api/diagnostics."""

    def _make_coord_with_counter(self):
        coord_mod = _import_coordinator_module()
        coord = coord_mod.Coordinator.__new__(coord_mod.Coordinator)
        coord._near_expiry_skip_count = {"paper": 0, "live": 0}
        coord._tick_count = 0
        return coord

    def test_counter_increments_per_mode(self):
        coord = self._make_coord_with_counter()
        state = SimpleNamespace(time_remaining_sec=None, kalshi_ticker="KXBTC-X")
        with patch("coordinator.settings") as s:
            s.bot.min_seconds_to_expiry = 120
            coord._log_near_expiry_skip(state, "live")
            coord._log_near_expiry_skip(state, "live")
            coord._log_near_expiry_skip(state, "paper")
        assert coord._near_expiry_skip_count == {"paper": 1, "live": 2}

    def test_counter_does_not_crash_when_ticker_missing(self):
        """Defensive: a state without kalshi_ticker should still log."""
        coord = self._make_coord_with_counter()
        state = SimpleNamespace(time_remaining_sec=None)  # no kalshi_ticker
        with patch("coordinator.settings") as s:
            s.bot.min_seconds_to_expiry = 120
            coord._log_near_expiry_skip(state, "live")  # must not raise
        assert coord._near_expiry_skip_count["live"] == 1


# ══════════════════════════════════════════════════════════════════════
# Layer 2: PositionManager._market_open_for_entry
# ══════════════════════════════════════════════════════════════════════


class TestMarketOpenForEntry:
    """Backstop check inside position_manager.enter()."""

    @pytest.mark.parametrize("status", ["open", "active", "Active", "OPEN"])
    async def test_returns_true_for_tradeable_statuses(self, status):
        """BUG-030: Kalshi returns ``"active"`` for most KXBTC contracts.
        Both ``open`` and ``active`` (and any unknown future status) must
        be accepted so we don't silently block entries."""
        pm = _make_pm()
        pm.client.get_market = AsyncMock(
            return_value={"market": {"status": status, "ticker": "KXBTC-T"}},
        )
        assert await pm._market_open_for_entry("KXBTC-T") is True

    @pytest.mark.parametrize("status", [
        "closed", "closing", "settled", "finalized",
        "determined", "halted", "CLOSED", "Settled",
    ])
    async def test_returns_false_for_terminal_statuses(self, status):
        """Both lower-case and mixed-case must be rejected — Kalshi has
        used both spellings historically and the comparison is case-
        insensitive.
        """
        pm = _make_pm()
        pm.client.get_market = AsyncMock(
            return_value={"market": {"status": status}},
        )
        assert await pm._market_open_for_entry("KXBTC-T") is False

    async def test_returns_true_on_rest_failure_fail_open(self):
        """If the recheck call itself errors (5xx, timeout), we
        proceed. Layer 1 is the primary defense; we don't want to
        introduce a new class of avoidable misses for transient blips.
        """
        pm = _make_pm()
        pm.client.get_market = AsyncMock(side_effect=httpx.ReadTimeout("boom"))
        assert await pm._market_open_for_entry("KXBTC-T") is True

    async def test_handles_payload_without_market_wrapper(self):
        """Some Kalshi endpoints return ``{status: ...}`` directly; the
        helper should accept both shapes without raising.
        """
        pm = _make_pm()
        pm.client.get_market = AsyncMock(
            return_value={"status": "closed", "ticker": "KXBTC-T"},
        )
        assert await pm._market_open_for_entry("KXBTC-T") is False

    async def test_empty_status_treated_as_unknown_passes(self):
        """If Kalshi returns no status field at all, we treat it as
        unknown rather than closed -- otherwise a Kalshi schema
        change could brick all entries silently. This matches the
        fail-open posture of the helper.
        """
        pm = _make_pm()
        pm.client.get_market = AsyncMock(return_value={"market": {}})
        assert await pm._market_open_for_entry("KXBTC-T") is True


# ══════════════════════════════════════════════════════════════════════
# Layer 2 wiring: enter() actually calls _market_open_for_entry
# ══════════════════════════════════════════════════════════════════════


class TestEnterUsesMarketStatusGuard:
    """End-to-end: enter() must invoke the guard and abort cleanly when
    the market is no longer open.
    """

    async def test_enter_aborts_when_market_status_not_open(self):
        pm = _make_pm()
        ticker = "KXBTC-26APR2416-B77550"

        # Pre-flight verify says we're flat.
        pm.client.get_positions = AsyncMock(
            return_value={"market_positions": []},
        )
        pm.client.get_balance = AsyncMock(return_value={"balance": 100000})

        # The bug: market closed in the millis between coordinator
        # decision and order placement. Recheck must catch it.
        pm.client.get_market = AsyncMock(
            return_value={"market": {"status": "closed"}},
        )

        # If we DON'T abort, create_order will be called -- assert
        # below confirms it wasn't.
        pm.client.create_order = AsyncMock(return_value=_order_resp())

        result = await pm.enter(
            ticker=ticker, direction="long", contracts=3, price=20,
            conviction="NORMAL", regime="MEDIUM",
        )

        assert result is None
        pm.client.create_order.assert_not_called()
        # State must end up FLAT (not stuck in ENTERING) so the next
        # tick can re-evaluate cleanly.
        assert pm.state == PositionState.FLAT

    async def test_enter_proceeds_when_market_status_is_open(self):
        """Sanity check -- the guard must not over-block when the
        market is actually open.
        """
        pm = _make_pm()
        ticker = "KXBTC-T"

        pm.client.get_positions = AsyncMock(side_effect=[
            {"market_positions": []},                  # pre-entry verify: flat
            _positions_resp(ticker, 3.0),              # post-entry verify: 3 contracts
        ])
        pm.client.get_balance = AsyncMock(return_value={"balance": 100000})
        pm.client.get_market = AsyncMock(
            return_value={"market": {"status": "open"}},
        )
        pm.client.create_order = AsyncMock(return_value=_order_resp("ord-1"))
        pm.client.get_order = AsyncMock(
            return_value=_get_order_resp(status="executed", filled=3),
        )

        result = await pm.enter(
            ticker=ticker, direction="long", contracts=3, price=20,
            conviction="NORMAL", regime="MEDIUM",
        )

        assert result is not None
        pm.client.create_order.assert_called_once()
        pm.client.get_market.assert_called_once_with(ticker)

    async def test_enter_proceeds_when_recheck_fails(self):
        """Fail-open posture: a transient REST failure must NOT block
        the entry. Layer 1 already filtered the obvious cases; we
        don't want to convert one rare race into a different rare
        miss.
        """
        pm = _make_pm()
        ticker = "KXBTC-T"

        pm.client.get_positions = AsyncMock(side_effect=[
            {"market_positions": []},
            _positions_resp(ticker, 3.0),
        ])
        pm.client.get_balance = AsyncMock(return_value={"balance": 100000})
        pm.client.get_market = AsyncMock(side_effect=httpx.ReadTimeout("network"))
        pm.client.create_order = AsyncMock(return_value=_order_resp("ord-1"))
        pm.client.get_order = AsyncMock(
            return_value=_get_order_resp(status="executed", filled=3),
        )

        result = await pm.enter(
            ticker=ticker, direction="long", contracts=3, price=20,
            conviction="NORMAL", regime="MEDIUM",
        )

        assert result is not None
        pm.client.create_order.assert_called_once()

    async def test_enter_skips_recheck_when_disabled_via_env(self):
        """Operator must be able to disable Layer 2 via env without a
        deploy if it's ever shown to add latency that costs more than
        the EXPIRY_409 trades it prevents.
        """
        pm = _make_pm()
        ticker = "KXBTC-T"

        pm.client.get_positions = AsyncMock(side_effect=[
            {"market_positions": []},
            _positions_resp(ticker, 3.0),
        ])
        pm.client.get_balance = AsyncMock(return_value={"balance": 100000})
        pm.client.get_market = AsyncMock(
            return_value={"market": {"status": "closed"}},  # would block normally
        )
        pm.client.create_order = AsyncMock(return_value=_order_resp("ord-1"))
        pm.client.get_order = AsyncMock(
            return_value=_get_order_resp(status="executed", filled=3),
        )

        with patch("execution.position_manager.settings") as s:
            s.bot.expiry_market_status_check_enabled = False

            result = await pm.enter(
                ticker=ticker, direction="long", contracts=3, price=20,
                conviction="NORMAL", regime="MEDIUM",
            )

        # With the recheck disabled, the entry must proceed even with
        # a "closed" status response, and create_order must be called.
        assert result is not None
        pm.client.create_order.assert_called_once()
        pm.client.get_market.assert_not_called()


# ══════════════════════════════════════════════════════════════════════
# Layer 4: DataManager._notify seeds state.expiry_time from REST
# ══════════════════════════════════════════════════════════════════════


def _import_data_manager_module():
    """Import data.manager without pulling in the full WS / fastapi /
    psycopg stack. Same pattern as ``_import_coordinator_module``.
    """
    import sys
    import types

    stubs = {
        "websockets": types.ModuleType("websockets"),
        "websockets.protocol": types.ModuleType("websockets.protocol"),
        "data.spot_client": types.ModuleType("data.spot_client"),
        "data.kalshi_ws": types.ModuleType("data.kalshi_ws"),
    }
    stubs["data.spot_client"].SpotPriceClient = MagicMock
    stubs["data.kalshi_ws"].KalshiWebSocketClient = MagicMock

    saved = {k: sys.modules.get(k) for k in stubs}
    for k, v in stubs.items():
        sys.modules[k] = v
    sys.modules.pop("data.manager", None)
    try:
        return __import__("data.manager", fromlist=["DataManager", "MarketState"])
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


class TestExpiryTimeSeedingFromREST:
    """The Layer 1 guard would block forever if ``state.expiry_time``
    never populated. Production observed this exact pathology: after a
    bot restart the Kalshi ``ticker`` channel did not deliver a
    ``ticker`` event for the active contract, so the previous code (which
    only set ``expiry_time`` on a ticker WS event) left the field None
    indefinitely and Layer 1 blocked every entry.

    The Layer 4 fix: ``DataManager._notify`` falls back to the
    ``close_time`` that ``KalshiWebSocketClient._resolve_tickers`` has
    *already* fetched via REST (it stores them in
    ``_active_close_times``). Spot ticks fire ~1x/sec so the fallback
    seeds within a second of ticker resolution.
    """

    def _make_state_and_dm(
        self, *, symbol: str, ticker: str | None, close_time_str: str | None,
        existing_expiry=None,
    ):
        from datetime import datetime, timezone
        mod = _import_data_manager_module()
        DataManager = mod.DataManager
        MarketState = mod.MarketState

        dm = DataManager.__new__(DataManager)
        dm._listeners = []
        dm._spot_ws = None
        dm._tick_task = None

        ws_stub = SimpleNamespace(
            active_tickers={symbol: ticker} if ticker else {},
            _active_close_times={symbol: close_time_str} if close_time_str else {},
        )
        dm._kalshi_ws = ws_stub

        state = MarketState(symbol=symbol)
        state.expiry_time = existing_expiry
        state.kalshi_ticker = None
        dm.states = {symbol: state}
        return dm, state, MarketState

    def test_seeds_expiry_time_when_unset_and_close_time_available(self):
        """The exact production pathology: ticker just rotated, no
        ``ticker`` WS event yet, but REST ``close_time`` is already in
        ``_active_close_times``. ``_notify`` must seed
        ``state.expiry_time`` from REST.
        """
        from datetime import datetime, timezone
        future_close = "2099-01-01T00:00:00Z"
        dm, state, _ = self._make_state_and_dm(
            symbol="BTC", ticker="KXBTC-T", close_time_str=future_close,
        )

        dm._notify("BTC", state)

        assert state.expiry_time is not None
        assert state.expiry_time.tzinfo is not None
        assert state.expiry_time.year == 2099
        assert state.kalshi_ticker == "KXBTC-T"
        assert state.time_remaining_sec is not None
        assert state.time_remaining_sec > 0

    def test_does_not_overwrite_fresher_expiry_time(self):
        """If a ``ticker`` channel event has already populated
        ``expiry_time`` (the fast path), the REST fallback must not
        clobber it -- the ticker event is the source of truth, REST is
        only the fallback for when the WS event never arrives.
        """
        from datetime import datetime, timezone
        ws_value = datetime(2050, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        rest_value_str = "2099-01-01T00:00:00Z"
        dm, state, _ = self._make_state_and_dm(
            symbol="BTC", ticker="KXBTC-T", close_time_str=rest_value_str,
            existing_expiry=ws_value,
        )

        dm._notify("BTC", state)

        assert state.expiry_time == ws_value

    def test_clears_stale_expiry_when_ticker_rotates(self):
        """When ``state.kalshi_ticker`` rotates from contract A to
        contract B, the previous contract's ``expiry_time`` must be
        cleared so the REST fallback re-seeds. Otherwise the bot would
        carry contract A's expiry (now in the past) onto contract B's
        position state -- and ``time_remaining_sec`` would clamp to 0,
        triggering the Layer 1 guard for the wrong reason.
        """
        from datetime import datetime, timezone, timedelta
        old_expiry = datetime.now(timezone.utc) - timedelta(minutes=5)
        new_close = (datetime.now(timezone.utc) + timedelta(minutes=30))
        new_close_str = new_close.strftime("%Y-%m-%dT%H:%M:%SZ")

        dm, state, _ = self._make_state_and_dm(
            symbol="BTC", ticker="KXBTC-NEW", close_time_str=new_close_str,
            existing_expiry=old_expiry,
        )
        state.kalshi_ticker = "KXBTC-OLD"

        dm._notify("BTC", state)

        assert state.kalshi_ticker == "KXBTC-NEW"
        assert state.expiry_time is not None
        assert state.expiry_time > datetime.now(timezone.utc)

    def test_handles_missing_close_time_gracefully(self):
        """If ``_active_close_times`` is empty (e.g. very early
        startup before ``_resolve_tickers`` has completed once),
        ``_notify`` must not raise -- it just leaves ``expiry_time``
        None, and Layer 1 continues to block entries until either WS
        or REST data arrives.
        """
        dm, state, _ = self._make_state_and_dm(
            symbol="BTC", ticker="KXBTC-T", close_time_str=None,
        )

        dm._notify("BTC", state)

        assert state.expiry_time is None
        assert state.kalshi_ticker == "KXBTC-T"

    def test_handles_malformed_close_time_gracefully(self):
        """A malformed close_time string from REST must not crash
        ``_notify``. The fallback silently no-ops and Layer 1 continues
        to gate entries.
        """
        dm, state, _ = self._make_state_and_dm(
            symbol="BTC", ticker="KXBTC-T", close_time_str="not-a-timestamp",
        )

        dm._notify("BTC", state)

        assert state.expiry_time is None

    def test_no_kalshi_ws_attached(self):
        """``_notify`` must be safe to call before ``start()`` has
        attached a ``_kalshi_ws``. Used by some test paths and during
        very early bootstrap.
        """
        mod = _import_data_manager_module()
        DataManager = mod.DataManager
        MarketState = mod.MarketState

        dm = DataManager.__new__(DataManager)
        dm._listeners = []
        dm._spot_ws = None
        dm._tick_task = None
        dm._kalshi_ws = None
        state = MarketState(symbol="BTC")
        dm.states = {"BTC": state}

        dm._notify("BTC", state)
        assert state.expiry_time is None
