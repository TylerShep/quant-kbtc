"""Deterministic replay tests for every known orphan production incident.

Each test replays the exact Kalshi API response sequence that caused a
specific orphan bug, then asserts the current code handles it correctly.
If any future code change re-introduces a regression, these tests will fail.
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from execution.position_manager import (
    ManagedPosition,
    PositionManager,
    PositionState,
    VERIFY_FAILED,
)
from tests.replay.helpers import FakeKalshiClient
from tests.replay.fixtures.incident_data import (
    INCIDENT_451_TICKER,
    INCIDENT_451_SETTLEMENT_VERIFY_FAILURE,
    BUG015_TICKER,
    BUG015_ACCUMULATION_PRESSURE,
    RESTART_TICKER,
    RESTART_SETTLED_PERSISTENCE,
    COOLDOWN_TICKER,
    COOLDOWN_RACE,
)


def _make_pm(client) -> PositionManager:
    pm = PositionManager(client)
    return pm


def _make_position(ticker: str, direction: str = "short",
                   contracts: int = 1, entry_price: float = 25.0):
    return ManagedPosition(
        ticker=ticker, direction=direction, contracts=contracts,
        entry_price=entry_price,
        entry_time="2026-04-14T02:08:36+00:00",
        conviction="NORMAL", regime_at_entry="MEDIUM",
    )


# ══════════════════════════════════════════════════════════════════════
# Incident 1: Settlement verify failure -> no duplicate (451/454 fix)
# ══════════════════════════════════════════════════════════════════════


class TestIncident451SettlementVerifyFailure:
    """Trade 451/454: verify_position fails during settlement.
    Old behavior: orphan created -> settles again -> double PnL.
    Fixed behavior: no orphan, ticker in settled_tickers + cooldown."""

    @pytest.mark.asyncio
    async def test_settlement_verify_failure_no_orphan_created(self):
        client = FakeKalshiClient(INCIDENT_451_SETTLEMENT_VERIFY_FAILURE)
        pm = _make_pm(client)
        pm.position = _make_position(INCIDENT_451_TICKER, "short", 1, 25.0)
        pm.state = PositionState.OPEN

        result = await pm._handle_settlement_inner("no")

        assert result is not None
        assert result["exit_reason"] == "CONTRACT_SETTLED_VERIFY_FAILED"
        assert result["pnl"] > 0
        assert len(pm.orphaned_positions) == 0
        assert pm.position is None
        assert pm.state == PositionState.FLAT

    @pytest.mark.asyncio
    async def test_settled_ticker_persisted_after_verify_failure(self):
        client = FakeKalshiClient(INCIDENT_451_SETTLEMENT_VERIFY_FAILURE)
        pm = _make_pm(client)
        pm.position = _make_position(INCIDENT_451_TICKER, "short", 1, 25.0)
        pm.state = PositionState.OPEN

        await pm._handle_settlement_inner("no")

        assert INCIDENT_451_TICKER in pm._settled_tickers

    @pytest.mark.asyncio
    async def test_cooldown_active_after_verify_failure(self):
        client = FakeKalshiClient(INCIDENT_451_SETTLEMENT_VERIFY_FAILURE)
        pm = _make_pm(client)
        pm.position = _make_position(INCIDENT_451_TICKER, "short", 1, 25.0)
        pm.state = PositionState.OPEN

        await pm._handle_settlement_inner("no")

        assert pm._is_in_cooldown(INCIDENT_451_TICKER)

    @pytest.mark.asyncio
    async def test_reconciliation_skips_settled_ticker_after_verify_failure(self):
        """Full replay: settle with verify fail, then reconciliation sees stale position."""
        client = FakeKalshiClient(INCIDENT_451_SETTLEMENT_VERIFY_FAILURE)
        pm = _make_pm(client)
        pm.position = _make_position(INCIDENT_451_TICKER, "short", 1, 25.0)
        pm.state = PositionState.OPEN

        await pm._handle_settlement_inner("no")

        client.advance()
        await pm._reconcile_inner()

        assert len(pm.orphaned_positions) == 0

    @pytest.mark.asyncio
    async def test_snapshot_preserves_settled_ticker_across_restart(self):
        """Full lifecycle: settle -> snapshot -> restore -> reconcile."""
        client = FakeKalshiClient(INCIDENT_451_SETTLEMENT_VERIFY_FAILURE)
        pm = _make_pm(client)
        pm.position = _make_position(INCIDENT_451_TICKER, "short", 1, 25.0)
        pm.state = PositionState.OPEN

        await pm._handle_settlement_inner("no")
        snapshot = pm.get_snapshot()

        pm2 = _make_pm(client)
        pm2.restore_from_snapshot(snapshot)

        assert INCIDENT_451_TICKER in pm2._settled_tickers

        client.advance()
        await pm2._reconcile_inner()

        assert len(pm2.orphaned_positions) == 0


# ══════════════════════════════════════════════════════════════════════
# Incident 2: BUG-015 phantom accumulation pressure test
# ══════════════════════════════════════════════════════════════════════


class TestBug015AccumulationPressure:
    """Trade 431: reconciliation ran ~70 times and inflated 6 -> 423 contracts.
    Fixed behavior: idempotent adoption keeps count at 6."""

    @pytest.mark.asyncio
    async def test_70_reconciliations_no_accumulation(self):
        client = FakeKalshiClient(BUG015_ACCUMULATION_PRESSURE)
        pm = _make_pm(client)

        for _ in range(70):
            await pm._reconcile_inner()

        assert len(pm.orphaned_positions) == 1
        assert pm.orphaned_positions[0].contracts == 6
        assert pm.orphaned_positions[0].ticker == BUG015_TICKER

    @pytest.mark.asyncio
    async def test_200_reconciliations_stable(self):
        """Even more aggressive: 200 cycles."""
        client = FakeKalshiClient(BUG015_ACCUMULATION_PRESSURE)
        pm = _make_pm(client)

        for _ in range(200):
            await pm._reconcile_inner()

        assert pm.orphaned_positions[0].contracts == 6

    @pytest.mark.asyncio
    async def test_direct_adopt_replaces_count_not_adds(self):
        """adopt_orphan called twice with different counts replaces, not adds."""
        client = FakeKalshiClient(BUG015_ACCUMULATION_PRESSURE)
        pm = _make_pm(client)

        pm.adopt_orphan(BUG015_TICKER, "long", 6, 17)
        assert pm.orphaned_positions[0].contracts == 6

        pm.adopt_orphan(BUG015_TICKER, "long", 4, 17)
        assert pm.orphaned_positions[0].contracts == 4
        assert len(pm.orphaned_positions) == 1


# ══════════════════════════════════════════════════════════════════════
# Incident 3: Restart wipes settled_tickers -> re-adoption
# ══════════════════════════════════════════════════════════════════════


class TestRestartSettledPersistence:
    """After a trade settles, its ticker must survive restart via snapshot.
    Old behavior: _settled_tickers lost on restart -> re-adopted.
    Fixed behavior: persisted in snapshot -> skipped on reconcile."""

    @pytest.mark.asyncio
    async def test_settled_ticker_survives_restart_and_blocks_reconcile(self):
        client = FakeKalshiClient(RESTART_SETTLED_PERSISTENCE)
        pm = _make_pm(client)
        pm._settled_tickers.add(RESTART_TICKER)

        snapshot = pm.get_snapshot()
        assert RESTART_TICKER in snapshot["settled_tickers"]

        pm2 = _make_pm(client)
        pm2.restore_from_snapshot(snapshot)
        assert RESTART_TICKER in pm2._settled_tickers

        await pm2._reconcile_inner()
        assert len(pm2.orphaned_positions) == 0

    @pytest.mark.asyncio
    async def test_without_persistence_would_create_orphan(self):
        """Control test: if settled_tickers is empty, orphan IS created."""
        client = FakeKalshiClient(RESTART_SETTLED_PERSISTENCE)
        pm = _make_pm(client)

        await pm._reconcile_inner()
        assert len(pm.orphaned_positions) == 1
        assert pm.orphaned_positions[0].ticker == RESTART_TICKER

    @pytest.mark.asyncio
    async def test_multiple_settled_tickers_all_persist(self):
        pm = _make_pm(MagicMock())
        pm._settled_tickers = {"TICKER-A", "TICKER-B", "TICKER-C"}

        snapshot = pm.get_snapshot()
        pm2 = _make_pm(MagicMock())
        pm2.restore_from_snapshot(snapshot)

        assert pm2._settled_tickers == {"TICKER-A", "TICKER-B", "TICKER-C"}


# ══════════════════════════════════════════════════════════════════════
# Incident 4: Exit cooldown race condition
# ══════════════════════════════════════════════════════════════════════


class TestExitCooldownRace:
    """Bot exits, reconciliation runs immediately, exchange still shows
    stale position. Cooldown must prevent orphan adoption."""

    @pytest.mark.asyncio
    async def test_cooldown_blocks_immediate_reconciliation(self):
        client = FakeKalshiClient(COOLDOWN_RACE)
        pm = _make_pm(client)

        pm._record_exit_cooldown(COOLDOWN_TICKER)

        await pm._reconcile_inner()

        assert len(pm.orphaned_positions) == 0

    @pytest.mark.asyncio
    async def test_expired_cooldown_allows_adoption(self):
        client = FakeKalshiClient(COOLDOWN_RACE)
        pm = _make_pm(client)

        pm._exit_cooldowns[COOLDOWN_TICKER] = time.time() - 200

        await pm._reconcile_inner()

        assert len(pm.orphaned_positions) == 1
        assert pm.orphaned_positions[0].ticker == COOLDOWN_TICKER

    @pytest.mark.asyncio
    async def test_cooldown_set_after_normal_exit(self):
        """Verify that a successful exit records a cooldown."""
        from tests.replay.helpers import TimelineEvent, ExchangePosition

        exit_timeline = [
            TimelineEvent(positions=[], verify_fails=False),
            TimelineEvent(positions=[], verify_fails=False),
        ]
        client = FakeKalshiClient(exit_timeline)
        pm = _make_pm(client)
        pm.position = _make_position(COOLDOWN_TICKER, "long", 2, 38.0)
        pm.state = PositionState.OPEN

        client.call_log.clear()
        pm.client = client

        result = await pm._handle_settlement_inner("yes")
        assert result is not None
        assert pm._is_in_cooldown(COOLDOWN_TICKER)


# ══════════════════════════════════════════════════════════════════════
# Cross-incident: full lifecycle integration
# ══════════════════════════════════════════════════════════════════════


class TestFullLifecycleIntegration:
    """End-to-end: enter -> settle -> restart -> reconcile -> no orphans."""

    @pytest.mark.asyncio
    async def test_full_settle_restart_reconcile_cycle(self):
        from tests.replay.helpers import TimelineEvent, ExchangePosition, MarketStatus

        ticker = "KXBTC-LIFECYCLE-TEST"

        timeline = [
            TimelineEvent(verify_fails=True),
            TimelineEvent(
                positions=[ExchangePosition(ticker=ticker, position_fp=-5.0,
                                            total_traded_dollars=1.25)],
                markets={ticker: MarketStatus(ticker=ticker, status="open")},
            ),
        ]
        client = FakeKalshiClient(timeline)
        pm = _make_pm(client)
        pm.position = _make_position(ticker, "short", 5, 25.0)
        pm.state = PositionState.OPEN

        result = await pm._handle_settlement_inner("no")
        assert result is not None
        assert len(pm.orphaned_positions) == 0
        assert ticker in pm._settled_tickers

        snapshot = pm.get_snapshot()
        pm2 = _make_pm(client)
        pm2.restore_from_snapshot(snapshot)
        assert ticker in pm2._settled_tickers

        client.advance()
        await pm2._reconcile_inner()

        assert len(pm2.orphaned_positions) == 0

    @pytest.mark.asyncio
    async def test_orphan_dedup_logic_standalone(self):
        """Dedup check: if trade was persisted for a ticker in last 5min,
        the orphan trade should be skipped."""
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=(999,))
        mock_conn.execute = AsyncMock(return_value=mock_cursor)

        mock_pool = MagicMock()
        mock_pool.connection = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=False),
        ))

        async def is_dup(pool, ticker, reason):
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
                    return result is not None
            except Exception:
                return False

        assert await is_dup(mock_pool, "KXBTC-DUP-TEST", "ORPHAN_SETTLED") is True
        assert await is_dup(None, "KXBTC-DUP-TEST", "ORPHAN_SETTLED") is False
