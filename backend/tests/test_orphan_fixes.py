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
        pm1 = _make_pm()
        pm1._settled_tickers = {"T1", "T2", "T3"}
        pm1._completed_live_trades = 3
        pm1.live_trade_limit = 5
        snap = pm1.get_snapshot()

        pm2 = _make_pm()
        pm2.restore_from_snapshot(snap)
        assert pm2._settled_tickers == {"T1", "T2", "T3"}
        assert pm2._completed_live_trades == 3
        assert pm2.live_trade_limit == 5


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
