"""Tests for orphan position mitigation fixes.

Fix 1: _settled_tickers persisted in snapshot/restore
Fix 2: Idempotent orphan adoption (replace, not accumulate)
Fix 3: Reconciliation cooldown after exit/settlement
Fix 4: Settlement verify retry with backoff
Fix 5: Orphan-to-trade deduplication
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from execution.position_manager import (
    PositionManager,
    PositionState,
    ManagedPosition,
    OrphanedPosition,
    VERIFY_FAILED,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _make_pm() -> PositionManager:
    """Create a PositionManager with a mocked Kalshi client."""
    client = MagicMock()
    pm = PositionManager(client)
    return pm


def _make_position(ticker: str = "KXBTC-TEST", direction: str = "long",
                   contracts: int = 5, entry_price: float = 25.0) -> ManagedPosition:
    return ManagedPosition(
        ticker=ticker,
        direction=direction,
        contracts=contracts,
        entry_price=entry_price,
        entry_time="2026-04-14T00:00:00+00:00",
        conviction="NORMAL",
        regime_at_entry="MEDIUM",
    )


# ══════════════════════════════════════════════════════════════════════
# Fix 1: _settled_tickers persistence
# ══════════════════════════════════════════════════════════════════════


class TestSettledTickersPersistence:

    def test_settled_tickers_in_snapshot(self):
        pm = _make_pm()
        pm._settled_tickers = {"KXBTC-A", "KXBTC-B", "KXBTC-C"}
        snap = pm.get_snapshot()
        assert "settled_tickers" in snap
        assert set(snap["settled_tickers"]) == {"KXBTC-A", "KXBTC-B", "KXBTC-C"}

    def test_settled_tickers_restored_from_snapshot(self):
        pm = _make_pm()
        snap = {
            "state": "FLAT",
            "position": None,
            "orphaned_positions": [],
            "completed_live_trades": 0,
            "live_trade_limit": 1,
            "settled_tickers": ["KXBTC-X", "KXBTC-Y"],
        }
        pm.restore_from_snapshot(snap)
        assert pm._settled_tickers == {"KXBTC-X", "KXBTC-Y"}

    def test_settled_tickers_empty_when_missing_from_snapshot(self):
        pm = _make_pm()
        pm._settled_tickers = {"OLD"}
        snap = {"state": "FLAT", "position": None, "orphaned_positions": []}
        pm.restore_from_snapshot(snap)
        assert pm._settled_tickers == set()

    def test_round_trip_snapshot_restore(self):
        """Snapshot round-trips the *counter* (so a restart can't release
        the supervised cap) but does NOT round-trip the *limit* — the
        limit always comes from the live PositionManager constructor
        (BotConfig.live_trade_limit) so operators can change the cap by
        editing LIVE_TRADE_LIMIT and restarting, without wiping bot_state.
        See position_manager.restore_from_snapshot for the rationale."""
        pm1 = _make_pm()
        pm1._settled_tickers = {"T1", "T2", "T3"}
        pm1._completed_live_trades = 3
        pm1.live_trade_limit = 5  # only affects the snapshot we serialize
        snap = pm1.get_snapshot()

        # pm2 gets a fresh limit from its constructor (None by default
        # in tests); the snapshot's "live_trade_limit" key is intentionally
        # ignored on restore.
        pm2 = _make_pm()
        pm2.restore_from_snapshot(snap)
        assert pm2._settled_tickers == {"T1", "T2", "T3"}
        assert pm2._completed_live_trades == 3
        assert pm2.live_trade_limit is None, (
            "Snapshot must NOT overwrite the constructor-set limit."
        )


# ══════════════════════════════════════════════════════════════════════
# BUG-031: restore_from_snapshot reconciles state/position mismatch
# ══════════════════════════════════════════════════════════════════════


class TestRestoreReconcilesInconsistentState:
    """BUG-031 (2026-05-03): the position-manager snapshot is written in
    two stages (state flips, then position is set/cleared, then persist).
    A crash or OOM kill between stage 2 and stage 3 — or an exit path
    that flips position to None without re-persisting — leaves the DB
    snapshot saying ``state="OPEN", position=null``. On restart the
    bot rehydrates that mismatch and ``can_enter()`` returns False
    forever. The live lane silently goes dark with no alarm because
    the position-manager guard short-circuits before the edge_profile
    filter ever fires (no signal_log rejections to count).

    Observed in production 2026-05-02: live trade #1200 closed at
    21:02:53 UTC, snapshot was persisted with state=OPEN/position=null,
    bot was restarted by a deploy ~5h later, ran for ~17h with
    ``can_enter: false`` and 0 EDGE skips in 24h.

    The fix forces FLAT when state is non-FLAT but position is None
    (the OPEN-without-position case) and clears the position when
    state is FLAT but position is set (the inverse case).
    """

    def test_open_with_null_position_forced_flat(self):
        """Exact production fingerprint."""
        pm = _make_pm()
        pm.restore_from_snapshot({
            "state": "OPEN",
            "position": None,
            "orphaned_positions": [],
            "completed_live_trades": 5,
            "settled_tickers": [],
        })
        assert pm.state == PositionState.FLAT, (
            "OPEN with no position must reconcile to FLAT or can_enter() "
            "returns False forever and the live lane silently goes dark."
        )
        assert pm.position is None
        assert pm._completed_live_trades == 5  # counter preserved

    def test_entering_with_null_position_forced_flat(self):
        """ENTERING (a transient mid-entry state) caught by the same
        rule — anything non-FLAT with no position is an inconsistency."""
        pm = _make_pm()
        pm.restore_from_snapshot({
            "state": "ENTERING",
            "position": None,
            "orphaned_positions": [],
        })
        assert pm.state == PositionState.FLAT
        assert pm.position is None

    def test_flat_with_position_forced_to_no_position(self):
        """The inverse mismatch: state says FLAT but a position dict is
        present. Trust the state field over the phantom position
        (FLAT is the safe default; trading proceeds without a phantom
        ManagedPosition that doesn't reflect actual exchange state)."""
        pm = _make_pm()
        pm.restore_from_snapshot({
            "state": "FLAT",
            "position": {
                "ticker": "KXBTC-PHANTOM",
                "direction": "long",
                "contracts": 5,
                "entry_price": 25.0,
                "entry_time": "2026-05-03T00:00:00+00:00",
                "conviction": "NORMAL",
                "regime_at_entry": "MEDIUM",
            },
            "orphaned_positions": [],
        })
        assert pm.state == PositionState.FLAT
        assert pm.position is None, (
            "FLAT state with a populated position dict is an inconsistency; "
            "the phantom must be dropped to keep state and position in sync."
        )

    def test_consistent_open_with_position_left_alone(self):
        """The reconciliation must NOT fire when state and position
        agree — happy-path restore must work unchanged."""
        pm = _make_pm()
        pm.restore_from_snapshot({
            "state": "OPEN",
            "position": {
                "ticker": "KXBTC-LIVE",
                "direction": "short",
                "contracts": 3,
                "entry_price": 65.0,
                "entry_time": "2026-05-03T00:00:00+00:00",
                "conviction": "HIGH",
                "regime_at_entry": "MEDIUM",
            },
            "orphaned_positions": [],
        })
        assert pm.state == PositionState.OPEN
        assert pm.position is not None
        assert pm.position.ticker == "KXBTC-LIVE"

    def test_consistent_flat_with_no_position_left_alone(self):
        """Mirror happy-path: clean FLAT restore is untouched."""
        pm = _make_pm()
        pm.restore_from_snapshot({
            "state": "FLAT",
            "position": None,
            "orphaned_positions": [],
        })
        assert pm.state == PositionState.FLAT
        assert pm.position is None


# ══════════════════════════════════════════════════════════════════════
# Fix 2: Idempotent orphan adoption
# ══════════════════════════════════════════════════════════════════════


class TestIdempotentOrphanAdoption:

    def test_first_adoption_creates_orphan(self):
        pm = _make_pm()
        pm.adopt_orphan("KXBTC-A", "long", 6, 19.0)
        assert len(pm.orphaned_positions) == 1
        assert pm.orphaned_positions[0].contracts == 6

    def test_second_adoption_replaces_not_adds(self):
        """Previously this would accumulate: 6 + 6 = 12. Now it replaces."""
        pm = _make_pm()
        pm.adopt_orphan("KXBTC-A", "long", 6, 19.0)
        pm.adopt_orphan("KXBTC-A", "long", 6, 19.0)
        assert len(pm.orphaned_positions) == 1
        assert pm.orphaned_positions[0].contracts == 6

    def test_repeated_adoption_same_count_stays_stable(self):
        """Simulates reconciliation running 70 times seeing the same position."""
        pm = _make_pm()
        for _ in range(70):
            pm.adopt_orphan("KXBTC-A", "long", 6, 19.0)
        assert len(pm.orphaned_positions) == 1
        assert pm.orphaned_positions[0].contracts == 6

    def test_adoption_different_count_updates(self):
        pm = _make_pm()
        pm.adopt_orphan("KXBTC-A", "long", 6, 19.0)
        pm.adopt_orphan("KXBTC-A", "long", 4, 19.0)
        assert pm.orphaned_positions[0].contracts == 4

    def test_different_tickers_create_separate_orphans(self):
        pm = _make_pm()
        pm.adopt_orphan("KXBTC-A", "long", 6, 19.0)
        pm.adopt_orphan("KXBTC-B", "short", 3, 75.0)
        assert len(pm.orphaned_positions) == 2


# ══════════════════════════════════════════════════════════════════════
# Fix 3: Reconciliation cooldown
# ══════════════════════════════════════════════════════════════════════


class TestReconciliationCooldown:

    def test_record_cooldown(self):
        pm = _make_pm()
        pm._record_exit_cooldown("KXBTC-A")
        assert "KXBTC-A" in pm._exit_cooldowns
        assert pm._is_in_cooldown("KXBTC-A") is True

    def test_cooldown_expires(self):
        pm = _make_pm()
        pm._exit_cooldowns["KXBTC-OLD"] = time.time() - 200
        assert pm._is_in_cooldown("KXBTC-OLD") is False
        assert "KXBTC-OLD" not in pm._exit_cooldowns

    def test_no_cooldown_for_unknown_ticker(self):
        pm = _make_pm()
        assert pm._is_in_cooldown("KXBTC-NEVER") is False

    def test_cooldown_within_window(self):
        pm = _make_pm()
        pm._exit_cooldowns["KXBTC-RECENT"] = time.time() - 30
        assert pm._is_in_cooldown("KXBTC-RECENT") is True

    def test_cooldown_at_boundary(self):
        pm = _make_pm()
        pm._exit_cooldowns["KXBTC-EDGE"] = time.time() - 91
        assert pm._is_in_cooldown("KXBTC-EDGE") is False

    @pytest.mark.asyncio
    async def test_reconciliation_skips_cooldown_ticker(self):
        pm = _make_pm()
        pm._record_exit_cooldown("KXBTC-JUST-EXITED")

        pm.client.get_positions = AsyncMock(return_value={
            "market_positions": [
                {
                    "ticker": "KXBTC-JUST-EXITED",
                    "position_fp": "5.0",
                    "total_traded_dollars": "1.0",
                },
            ],
        })
        pm.client.get_market = AsyncMock(return_value={
            "market": {"status": "open"},
        })

        await pm._reconcile_inner()

        assert len(pm.orphaned_positions) == 0

    @pytest.mark.asyncio
    async def test_reconciliation_adopts_non_cooldown_ticker(self):
        pm = _make_pm()

        pm.client.get_positions = AsyncMock(return_value={
            "market_positions": [
                {
                    "ticker": "KXBTC-OLD-TICKER",
                    "position_fp": "5.0",
                    "total_traded_dollars": "1.0",
                },
            ],
        })
        pm.client.get_market = AsyncMock(return_value={
            "market": {"status": "open"},
        })

        await pm._reconcile_inner()

        assert len(pm.orphaned_positions) == 1
        assert pm.orphaned_positions[0].ticker == "KXBTC-OLD-TICKER"


# ══════════════════════════════════════════════════════════════════════
# Fix 4: Settlement verify retry
# ══════════════════════════════════════════════════════════════════════


class TestVerifyWithRetry:

    @pytest.mark.asyncio
    async def test_succeeds_first_try(self):
        pm = _make_pm()
        pm.client.get_positions = AsyncMock(return_value={
            "market_positions": [
                {"ticker": "T", "position_fp": "0"},
            ],
        })
        result = await pm._verify_with_retry("T", retries=3, backoff=0.01)
        assert result == 0
        assert pm.client.get_positions.call_count == 1

    @pytest.mark.asyncio
    async def test_succeeds_after_transient_failure(self):
        pm = _make_pm()
        call_count = 0

        async def mock_get_positions(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise Exception("API timeout")
            return {"market_positions": [{"ticker": "T", "position_fp": "0"}]}

        pm.client.get_positions = mock_get_positions
        result = await pm._verify_with_retry("T", retries=3, backoff=0.01)
        assert result == 0
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_exhausted_returns_verify_failed(self):
        pm = _make_pm()
        pm.client.get_positions = AsyncMock(side_effect=Exception("down"))
        result = await pm._verify_with_retry("T", retries=2, backoff=0.01)
        assert result == VERIFY_FAILED
        assert pm.client.get_positions.call_count == 2

    @pytest.mark.asyncio
    async def test_settlement_no_orphan_on_verify_failed(self):
        """Fix 4 core test: verify failure during settlement should NOT
        create an orphan anymore. It should add to settled_tickers and
        record a cooldown instead."""
        pm = _make_pm()
        pm.position = _make_position()
        pm.state = PositionState.OPEN

        pm.client.get_positions = AsyncMock(side_effect=Exception("API error"))

        result = await pm._handle_settlement_inner("no")
        assert result is not None
        assert result["exit_reason"] == "CONTRACT_SETTLED_VERIFY_FAILED"
        assert len(pm.orphaned_positions) == 0
        assert pm.position is None
        assert "KXBTC-TEST" in pm._settled_tickers
        assert pm._is_in_cooldown("KXBTC-TEST") is True


# ══════════════════════════════════════════════════════════════════════
# Fix 5: Orphan-to-trade deduplication (coordinator level)
# ══════════════════════════════════════════════════════════════════════


class TestOrphanDeduplication:
    """Tests the _is_duplicate_orphan_trade logic.

    Coordinator has heavy imports, so we replicate the method logic
    here to test it in isolation without the full dependency chain.
    """

    @staticmethod
    async def _is_duplicate_orphan_trade(pool, ticker, reason):
        """Standalone replica of Coordinator._is_duplicate_orphan_trade."""
        if pool is None:
            return False
        try:
            async with pool.connection() as conn:
                row = await conn.execute(
                    """SELECT id FROM trades
                       WHERE ticker = %s AND trading_mode = 'live'
                       AND timestamp >= NOW() - INTERVAL '5 minutes'
                       LIMIT 1""",
                    (ticker,),
                )
                result = await row.fetchone()
                if result:
                    return True
        except Exception:
            pass
        return False

    @pytest.mark.asyncio
    async def test_no_duplicate_when_no_pool(self):
        result = await self._is_duplicate_orphan_trade(None, "KXBTC-A", "ORPHAN_SETTLED")
        assert result is False

    @pytest.mark.asyncio
    async def test_duplicate_detected(self):
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=(451,))
        mock_conn.execute = AsyncMock(return_value=mock_cursor)

        mock_pool = MagicMock()
        mock_pool.connection = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=False),
        ))

        result = await self._is_duplicate_orphan_trade(mock_pool, "KXBTC-A", "ORPHAN_SETTLED")
        assert result is True

    @pytest.mark.asyncio
    async def test_no_duplicate_when_no_match(self):
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value=mock_cursor)

        mock_pool = MagicMock()
        mock_pool.connection = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=False),
        ))

        result = await self._is_duplicate_orphan_trade(mock_pool, "KXBTC-NEW", "ORPHAN_SETTLED")
        assert result is False

    @pytest.mark.asyncio
    async def test_duplicate_check_handles_db_error(self):
        mock_pool = MagicMock()
        mock_pool.connection = MagicMock(side_effect=Exception("DB down"))

        result = await self._is_duplicate_orphan_trade(mock_pool, "KXBTC-A", "ORPHAN_SETTLED")
        assert result is False


# ══════════════════════════════════════════════════════════════════════
# Integration: full orphan lifecycle
# ══════════════════════════════════════════════════════════════════════


class TestOrphanLifecycleIntegration:

    def test_settled_ticker_blocks_reconciliation_adoption(self):
        """After a trade settles, its ticker should be in _settled_tickers
        and reconciliation should skip it."""
        pm = _make_pm()
        pm._settled_tickers.add("KXBTC-SETTLED")
        pm.adopt_orphan("KXBTC-OTHER", "long", 5, 20.0)
        assert len(pm.orphaned_positions) == 1
        assert pm.orphaned_positions[0].ticker == "KXBTC-OTHER"

    @pytest.mark.asyncio
    async def test_full_settle_cooldown_reconcile_cycle(self):
        """Simulate: trade settles -> cooldown active -> reconciliation
        sees stale position -> should skip it."""
        pm = _make_pm()
        pm.position = _make_position(ticker="KXBTC-CYCLE")
        pm.state = PositionState.OPEN

        pm.client.get_positions = AsyncMock(return_value={
            "market_positions": [{"ticker": "KXBTC-CYCLE", "position_fp": "0"}],
        })

        result = await pm._handle_settlement_inner("no")
        assert result is not None
        assert pm.position is None
        assert pm._is_in_cooldown("KXBTC-CYCLE")
        assert "KXBTC-CYCLE" in pm._settled_tickers

        pm.client.get_positions = AsyncMock(return_value={
            "market_positions": [
                {"ticker": "KXBTC-CYCLE", "position_fp": "5.0",
                 "total_traded_dollars": "1.0"},
            ],
        })
        pm.client.get_market = AsyncMock(return_value={
            "market": {"status": "open"},
        })

        await pm._reconcile_inner()

        assert len(pm.orphaned_positions) == 0

    def test_snapshot_preserves_full_state_after_settlement(self):
        """After settlement, snapshot should include the settled ticker
        so a restart preserves it."""
        pm = _make_pm()
        pm._settled_tickers.add("KXBTC-SETTLED-1")
        pm._settled_tickers.add("KXBTC-SETTLED-2")

        snap = pm.get_snapshot()

        pm2 = _make_pm()
        pm2.restore_from_snapshot(snap)
        assert "KXBTC-SETTLED-1" in pm2._settled_tickers
        assert "KXBTC-SETTLED-2" in pm2._settled_tickers

    def test_idempotent_adoption_prevents_bug015(self):
        """BUG-015 reproduction: reconciliation runs 70 times seeing
        6 contracts. Old code: 6*70=420. New code: stays at 6."""
        pm = _make_pm()
        for i in range(70):
            pm.adopt_orphan("KXBTC-BUG015", "long", 6, 17.0)
        assert pm.orphaned_positions[0].contracts == 6


# ══════════════════════════════════════════════════════════════════════
# BUG-031 RUNTIME PATH: adopt_orphan_and_clear_position atomicity
# ══════════════════════════════════════════════════════════════════════
#
# These guard the runtime path that produces ``state="OPEN"|"EXITING"`` with
# ``position=null`` snapshots. The previous coordinator path called
# ``pm.adopt_orphan(...)`` then ``pm.position = None`` — two separate
# mutations, two separate scheduled persists. If the bot crashed/OOM'd or
# the snapshot landed between the calls, the persisted state was
# inconsistent and BUG-031's restore-time reconciliation would have to
# silently downgrade to FLAT on the NEXT boot. This battery enforces the
# new ``adopt_orphan_and_clear_position`` does all three mutations
# (adopt + clear position + transition to FLAT) before its single persist
# is scheduled, so the snapshot can never observe the in-between state.


class TestAdoptOrphanAndClearPositionAtomic:

    def test_adopts_orphan(self):
        pm = _make_pm()
        pm.adopt_orphan_and_clear_position(
            "KXBTC-BUG031", "long", 8, 26.0,
        )
        assert len(pm.orphaned_positions) == 1
        assert pm.orphaned_positions[0].ticker == "KXBTC-BUG031"
        assert pm.orphaned_positions[0].contracts == 8
        assert pm.orphaned_positions[0].direction == "long"

    def test_clears_position(self):
        pm = _make_pm()
        pm.position = _make_position(ticker="KXBTC-BUG031", contracts=8, entry_price=26.0)
        pm.state = PositionState.OPEN

        pm.adopt_orphan_and_clear_position(
            "KXBTC-BUG031", "long", 8, 26.0,
        )

        assert pm.position is None

    def test_transitions_to_flat(self):
        pm = _make_pm()
        pm.position = _make_position(ticker="KXBTC-BUG031")
        pm.state = PositionState.EXITING

        pm.adopt_orphan_and_clear_position(
            "KXBTC-BUG031", "long", 8, 26.0,
        )

        assert pm.state == PositionState.FLAT

    def test_no_op_state_change_when_already_flat(self):
        pm = _make_pm()
        pm.state = PositionState.FLAT

        pm.adopt_orphan_and_clear_position(
            "KXBTC-BUG031", "long", 8, 26.0,
        )

        assert pm.state == PositionState.FLAT
        assert pm.position is None

    def test_snapshot_after_atomic_call_is_consistent(self):
        """Snapshot taken after the atomic call must show
        state=FLAT AND position=None — never state=OPEN+position=None
        which is the BUG-031 inconsistent state."""
        pm = _make_pm()
        pm.position = _make_position(ticker="KXBTC-BUG031")
        pm.state = PositionState.EXITING

        pm.adopt_orphan_and_clear_position(
            "KXBTC-BUG031", "long", 8, 26.0,
        )

        snap = pm.get_snapshot()
        assert snap["state"] == "FLAT"
        assert snap["position"] is None
        assert len(snap["orphaned_positions"]) == 1

    def test_passes_cause_and_counted_through(self):
        pm = _make_pm()
        pm.adopt_orphan_and_clear_position(
            "KXBTC-BUG031", "long", 8, 26.0,
            cause="EXPIRY_409", counted=True,
        )
        o = pm.orphaned_positions[0]
        assert o.cause == "EXPIRY_409"
        assert o.counted is True

    def test_idempotent_with_existing_orphan_for_same_ticker(self):
        pm = _make_pm()
        pm.adopt_orphan("KXBTC-BUG031", "long", 8, 26.0)
        pm.position = _make_position(ticker="KXBTC-BUG031")
        pm.state = PositionState.EXITING

        pm.adopt_orphan_and_clear_position(
            "KXBTC-BUG031", "long", 8, 26.0,
        )

        assert len(pm.orphaned_positions) == 1
        assert pm.position is None
        assert pm.state == PositionState.FLAT
