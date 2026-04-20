"""Tests for the BUG-022 follow-up improvements:

(a) EXPIRY_409_SETTLED exit_reason — disambiguate orphans created by an
    expiry-time 409 Conflict from genuine phantom-fill orphans.
(b) Per-ticker entry cooldown after phantom_entry_prevented — stops the
    coordinator from immediately re-attempting the same ticker while the
    just-cancelled order is still bouncing through Kalshi's books.
(c) Supervised live_trade counter must advance on the orphan-recovery
    path so the live_trade_limit gate trips even for orphan-only round
    trips.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from execution.position_manager import (
    ManagedPosition,
    OrphanedPosition,
    PositionManager,
    PositionState,
)


# ── Speed-up fixture ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fast_clocks():
    """Stub asyncio.sleep + shrink poll constants so tests run in <1s."""

    async def _instant_sleep(_seconds: float) -> None:
        return None

    with patch("execution.position_manager.ENTRY_REST_BAILOUT_SEC", 0.0), \
         patch("execution.position_manager.ENTRY_CANCEL_SETTLE_SEC", 0.0), \
         patch("execution.position_manager.FILL_POLL_INTERVAL", 0.001), \
         patch("execution.position_manager.FILL_POLL_TIMEOUT", 0.05), \
         patch("execution.position_manager.asyncio.sleep", _instant_sleep):
        yield


# ── Helpers ──────────────────────────────────────────────────────────


def _make_pm() -> PositionManager:
    client = MagicMock()
    return PositionManager(client)


def _make_position(ticker: str = "KXBTC-T", direction: str = "long",
                   contracts: int = 5, entry_price: float = 25.0) -> ManagedPosition:
    return ManagedPosition(
        ticker=ticker,
        direction=direction,
        contracts=contracts,
        entry_price=entry_price,
        entry_time="2026-04-20T08:30:00+00:00",
        conviction="NORMAL",
        regime_at_entry="MEDIUM",
    )


# ══════════════════════════════════════════════════════════════════════
# (a) EXPIRY_409_SETTLED exit_reason
# ══════════════════════════════════════════════════════════════════════


class TestExpiry409SettledReason:
    """Settlement triggered by a 409 Conflict at expiry must be labelled
    EXPIRY_409_SETTLED, both on the immediate trade_result and on any
    orphan that the path produces (so check_orphans tags it correctly
    later)."""

    @pytest.mark.asyncio
    async def test_settlement_via_409_uses_expiry_label(self):
        pm = _make_pm()
        pm.position = _make_position(ticker="KXBTC-EXPIRY", contracts=3, entry_price=20.0)
        pm.state = PositionState.OPEN

        pm.client.get_positions = AsyncMock(return_value={
            "market_positions": [
                {"ticker": "KXBTC-EXPIRY", "position_fp": "0",
                 "total_traded_dollars": "0"},
            ],
        })

        result = await pm._handle_settlement_inner("no", via_409=True)
        assert result is not None
        assert result["exit_reason"] == "EXPIRY_409_SETTLED"
        assert result["ticker"] == "KXBTC-EXPIRY"

    @pytest.mark.asyncio
    async def test_settlement_without_409_uses_normal_label(self):
        pm = _make_pm()
        pm.position = _make_position(ticker="KXBTC-NORMAL", contracts=3, entry_price=20.0)
        pm.state = PositionState.OPEN

        pm.client.get_positions = AsyncMock(return_value={
            "market_positions": [
                {"ticker": "KXBTC-NORMAL", "position_fp": "0",
                 "total_traded_dollars": "0"},
            ],
        })

        result = await pm._handle_settlement_inner("yes")
        assert result is not None
        assert result["exit_reason"] == "CONTRACT_SETTLED"

    @pytest.mark.asyncio
    async def test_full_position_redirect_tags_orphan_with_cause(self):
        """When settlement finds the full position still on-exchange, the
        adopted orphan must carry cause=EXPIRY_409 if via_409 was True."""
        pm = _make_pm()
        pm.position = _make_position(ticker="KXBTC-FULL", contracts=4, entry_price=20.0)
        pm.state = PositionState.OPEN

        pm.client.get_positions = AsyncMock(return_value={
            "market_positions": [
                {"ticker": "KXBTC-FULL", "position_fp": "4",
                 "total_traded_dollars": "0"},
            ],
        })

        result = await pm._handle_settlement_inner("no", via_409=True)
        assert result is None
        assert len(pm.orphaned_positions) == 1
        assert pm.orphaned_positions[0].ticker == "KXBTC-FULL"
        assert pm.orphaned_positions[0].cause == "EXPIRY_409"

    @pytest.mark.asyncio
    async def test_full_position_redirect_without_409_has_no_cause(self):
        pm = _make_pm()
        pm.position = _make_position(ticker="KXBTC-FULL2", contracts=4, entry_price=20.0)
        pm.state = PositionState.OPEN

        pm.client.get_positions = AsyncMock(return_value={
            "market_positions": [
                {"ticker": "KXBTC-FULL2", "position_fp": "4",
                 "total_traded_dollars": "0"},
            ],
        })

        result = await pm._handle_settlement_inner("yes")
        assert result is None
        assert len(pm.orphaned_positions) == 1
        assert pm.orphaned_positions[0].cause is None

    @pytest.mark.asyncio
    async def test_check_orphans_uses_expiry_label_when_cause_set(self):
        pm = _make_pm()
        pm.adopt_orphan("KXBTC-CHECK", "long", 2, 20.0, cause="EXPIRY_409")

        pm.client.get_market = AsyncMock(return_value={
            "market": {
                "ticker": "KXBTC-CHECK",
                "status": "settled",
                "result": "no",
            },
        })

        closed = await pm._check_orphans_inner()
        assert len(closed) == 1
        assert closed[0]["reason"] == "EXPIRY_409_SETTLED"

    @pytest.mark.asyncio
    async def test_check_orphans_uses_orphan_label_when_no_cause(self):
        pm = _make_pm()
        pm.adopt_orphan("KXBTC-CHECK2", "long", 2, 20.0)

        pm.client.get_market = AsyncMock(return_value={
            "market": {
                "ticker": "KXBTC-CHECK2",
                "status": "settled",
                "result": "yes",
            },
        })

        closed = await pm._check_orphans_inner()
        assert len(closed) == 1
        assert closed[0]["reason"] == "ORPHAN_SETTLED"

    @pytest.mark.asyncio
    async def test_exit_inner_returns_via_409_marker(self):
        """The 409 → settlement redirect dict must carry _via_409=True so
        the outer exit() forwards the flag into _handle_settlement_inner."""
        pm = _make_pm()
        pm.position = _make_position(ticker="KXBTC-EXIT")
        pm.state = PositionState.OPEN

        pm.client.create_order = AsyncMock(
            side_effect=Exception("HTTP 409 Conflict: market closing"),
        )
        pm.client.get_market = AsyncMock(return_value={
            "market": {
                "ticker": "KXBTC-EXIT",
                "status": "settled",
                "result": "no",
            },
        })

        result = await pm._exit_inner(50.0, "EXPIRY_GUARD")
        assert result == {"_settled": True, "_result": "no", "_via_409": True}

    @pytest.mark.asyncio
    async def test_exit_outer_propagates_via_409_to_settlement(self):
        """End-to-end: exit() must forward via_409=True from _exit_inner
        into _handle_settlement_inner, producing EXPIRY_409_SETTLED."""
        pm = _make_pm()
        pm.position = _make_position(ticker="KXBTC-E2E", contracts=2,
                                     entry_price=30.0)
        pm.state = PositionState.OPEN

        pm.client.create_order = AsyncMock(
            side_effect=Exception("HTTP 409 Conflict"),
        )
        pm.client.get_market = AsyncMock(return_value={
            "market": {"ticker": "KXBTC-E2E", "status": "settled", "result": "no"},
        })
        pm.client.get_positions = AsyncMock(return_value={
            "market_positions": [
                {"ticker": "KXBTC-E2E", "position_fp": "0",
                 "total_traded_dollars": "0"},
            ],
        })

        result = await pm.exit(50.0, "EXPIRY_GUARD")
        assert result is not None
        assert result["exit_reason"] == "EXPIRY_409_SETTLED"

    def test_orphan_dataclass_defaults_preserve_legacy_construction(self):
        """Existing call sites that pre-date the cause/counted fields must
        keep working. asdict round-trip must include both new fields."""
        from dataclasses import asdict
        legacy = OrphanedPosition(
            ticker="KXBTC-OLD", direction="long", contracts=3,
            avg_entry_price=20.0, detected_at="2026-04-20T00:00:00+00:00",
        )
        assert legacy.cause is None
        assert legacy.counted is False
        d = asdict(legacy)
        assert d["cause"] is None
        assert d["counted"] is False


# ══════════════════════════════════════════════════════════════════════
# (b) Per-ticker entry cooldown after phantom_entry_prevented
# ══════════════════════════════════════════════════════════════════════


class TestPhantomEntryCooldown:

    def test_cooldown_starts_inactive(self):
        pm = _make_pm()
        assert pm._is_in_phantom_entry_cooldown("KXBTC-X") is False
        assert pm.can_enter_ticker("KXBTC-X") is True

    def test_record_cooldown_blocks_re_entry(self):
        pm = _make_pm()
        pm._record_phantom_entry_cooldown("KXBTC-X")
        assert pm._is_in_phantom_entry_cooldown("KXBTC-X") is True
        assert pm.can_enter_ticker("KXBTC-X") is False

    def test_other_tickers_unaffected(self):
        pm = _make_pm()
        pm._record_phantom_entry_cooldown("KXBTC-X")
        assert pm.can_enter_ticker("KXBTC-Y") is True

    def test_cooldown_expires_naturally(self):
        pm = _make_pm()
        pm.PHANTOM_ENTRY_COOLDOWN_SEC = 0.1
        pm._record_phantom_entry_cooldown("KXBTC-X")
        time.sleep(0.15)
        assert pm._is_in_phantom_entry_cooldown("KXBTC-X") is False
        assert "KXBTC-X" not in pm._entry_phantom_cooldowns

    def test_can_enter_ticker_respects_can_enter_too(self):
        """can_enter_ticker is the union of can_enter and per-ticker
        cooldown. It must reject when the global gate is closed even if
        no cooldown is set."""
        pm = _make_pm()
        pm.live_trade_limit = 2
        pm._completed_live_trades = 2
        assert pm.can_enter_ticker("KXBTC-X") is False

    @pytest.mark.asyncio
    async def test_phantom_entry_records_cooldown(self):
        """When enter() detects verified==0 (phantom prevention), it must
        register the cooldown so the next enter() on the same ticker is
        rejected immediately."""
        pm = _make_pm()
        # Force the trade-limit gate open and bypass exchange flat check.
        pm.live_trade_limit = None
        pm._check_flat_on_exchange = AsyncMock(return_value=True)

        pm.client.create_order = AsyncMock(return_value={
            "order": {"order_id": "ord-1"},
        })
        pm.client.get_order = AsyncMock(return_value={
            "order": {"status": "resting", "fill_count_fp": "0"},
        })
        pm.client.cancel_order = AsyncMock(return_value={})
        pm.client.get_positions = AsyncMock(return_value={
            "market_positions": [
                {"ticker": "KXBTC-PHANTOM", "position_fp": "0",
                 "total_traded_dollars": "0"},
            ],
        })

        result = await pm.enter(
            ticker="KXBTC-PHANTOM", direction="long", contracts=3,
            price=25.0, conviction="NORMAL", regime="MEDIUM",
        )
        assert result is None  # phantom_entry_prevented
        assert pm._is_in_phantom_entry_cooldown("KXBTC-PHANTOM") is True

    @pytest.mark.asyncio
    async def test_enter_rejected_during_cooldown(self):
        """A second enter() within the cooldown window short-circuits
        before placing any order on the exchange."""
        pm = _make_pm()
        pm.live_trade_limit = None
        pm._record_phantom_entry_cooldown("KXBTC-COOL")

        pm.client.create_order = AsyncMock()
        pm._check_flat_on_exchange = AsyncMock(return_value=True)

        result = await pm.enter(
            ticker="KXBTC-COOL", direction="long", contracts=3,
            price=25.0, conviction="NORMAL", regime="MEDIUM",
        )
        assert result is None
        pm.client.create_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_other_ticker_still_allowed_during_cooldown(self):
        """Cooldown is per-ticker; a different ticker proceeds normally."""
        pm = _make_pm()
        pm.live_trade_limit = None
        pm._record_phantom_entry_cooldown("KXBTC-A")
        pm._check_flat_on_exchange = AsyncMock(return_value=True)

        pm.client.create_order = AsyncMock(return_value={
            "order": {"order_id": "ord-B"},
        })
        pm.client.get_order = AsyncMock(return_value={
            "order": {"status": "executed", "fill_count_fp": "3",
                      "yes_price_dollars": "0.25",
                      "taker_fill_cost_dollars": "0.75",
                      "taker_fees_dollars": "0.005"},
        })
        pm.client.get_positions = AsyncMock(return_value={
            "market_positions": [
                {"ticker": "KXBTC-B", "position_fp": "3",
                 "total_traded_dollars": "0.75"},
            ],
        })

        pos = await pm.enter(
            ticker="KXBTC-B", direction="long", contracts=3,
            price=25.0, conviction="NORMAL", regime="MEDIUM",
        )
        assert pos is not None
        assert pos.ticker == "KXBTC-B"
        # KXBTC-A still in cooldown.
        assert pm._is_in_phantom_entry_cooldown("KXBTC-A") is True


# ══════════════════════════════════════════════════════════════════════
# (c) Supervised live_trade counter on orphan-recovery path
# ══════════════════════════════════════════════════════════════════════


class TestSupervisedCounterBumping:

    def test_bump_completed_trades_increments(self):
        pm = _make_pm()
        pm._completed_live_trades = 0
        pm.bump_completed_trades(1, source="test")
        assert pm._completed_live_trades == 1

    def test_bump_completed_trades_multi(self):
        pm = _make_pm()
        pm._completed_live_trades = 0
        pm.bump_completed_trades(3, source="test")
        assert pm._completed_live_trades == 3

    def test_bump_zero_or_negative_is_noop(self):
        pm = _make_pm()
        pm._completed_live_trades = 5
        pm.bump_completed_trades(0)
        pm.bump_completed_trades(-2)
        assert pm._completed_live_trades == 5

    def test_bump_returns_new_total(self):
        pm = _make_pm()
        pm._completed_live_trades = 0
        new_total = pm.bump_completed_trades(2)
        assert new_total == 2

    def test_bump_trips_can_enter_gate(self):
        """Once bumped to the limit, can_enter must return False."""
        pm = _make_pm()
        pm.live_trade_limit = 2
        pm._completed_live_trades = 0
        pm.bump_completed_trades(2)
        assert pm.can_enter is False

    @pytest.mark.asyncio
    async def test_settlement_orphan_redirect_bumps_counter(self):
        """When settlement adopts the full position as an orphan, the
        counter must bump even though the function returns None."""
        pm = _make_pm()
        pm.position = _make_position(ticker="KXBTC-RED", contracts=4)
        pm.state = PositionState.OPEN
        pm.live_trade_limit = 2
        pm._completed_live_trades = 0

        pm.client.get_positions = AsyncMock(return_value={
            "market_positions": [
                {"ticker": "KXBTC-RED", "position_fp": "4",
                 "total_traded_dollars": "0"},
            ],
        })

        result = await pm._handle_settlement_inner("no", via_409=True)
        assert result is None
        assert pm._completed_live_trades == 1

    @pytest.mark.asyncio
    async def test_settlement_orphan_redirect_marks_orphan_counted(self):
        pm = _make_pm()
        pm.position = _make_position(ticker="KXBTC-MARK", contracts=4)
        pm.state = PositionState.OPEN
        pm.client.get_positions = AsyncMock(return_value={
            "market_positions": [
                {"ticker": "KXBTC-MARK", "position_fp": "4",
                 "total_traded_dollars": "0"},
            ],
        })

        await pm._handle_settlement_inner("no")
        assert pm.orphaned_positions[0].counted is True

    @pytest.mark.asyncio
    async def test_check_orphans_surfaces_already_counted_flag(self):
        pm = _make_pm()
        # Pre-counted orphan (e.g., via settlement-redirect path)
        pm.adopt_orphan("KXBTC-PRE", "long", 2, 20.0,
                        cause="EXPIRY_409", counted=True)
        # Fresh orphan (e.g., real phantom-fill orphan from normal exit)
        pm.adopt_orphan("KXBTC-NEW", "long", 1, 30.0)

        async def get_market(ticker: str) -> dict:
            return {"market": {"ticker": ticker, "status": "settled", "result": "no"}}

        pm.client.get_market = AsyncMock(side_effect=get_market)

        closed = await pm._check_orphans_inner()
        flags = {info["ticker"]: info["already_counted"] for info in closed}
        assert flags["KXBTC-PRE"] is True
        assert flags["KXBTC-NEW"] is False

    @pytest.mark.asyncio
    async def test_partial_recovery_remainder_inherits_cause_and_locks_counter(self):
        """When the bid-match path partially fills an orphan, the first
        closure carries already_counted = original_orphan.counted (so the
        coordinator bumps once if needed), and the leftover remainder is
        always flagged counted=True so the second closure cannot bump
        again."""
        pm = _make_pm()
        # Original orphan was uncounted (not from settlement-redirect), so
        # the first closure should produce already_counted=False, but the
        # remainder must still be counted=True to lock out a second bump.
        pm.adopt_orphan("KXBTC-PART", "long", 5, 30.0, cause="EXPIRY_409")

        pm.client.get_market = AsyncMock(return_value={
            "market": {
                "ticker": "KXBTC-PART",
                "status": "open",
                "yes_bid": 35,
            },
        })
        pm.client.create_order = AsyncMock(return_value={
            "order": {"order_id": "ord-recovery"},
        })
        pm.client.get_order = AsyncMock(return_value={
            "order": {"status": "executed", "fill_count_fp": "3",
                      "yes_price_dollars": "0.35"},
        })

        closed = await pm._check_orphans_inner()
        assert len(closed) == 1
        assert closed[0]["contracts"] == 3
        # Original was uncounted → first closure asks coordinator to bump.
        assert closed[0]["already_counted"] is False
        # Remainder lives on as a new orphan; cause carries over, counted
        # is forced True so the second closure won't double-count.
        assert len(pm.orphaned_positions) == 1
        remainder = pm.orphaned_positions[0]
        assert remainder.contracts == 2
        assert remainder.cause == "EXPIRY_409"
        assert remainder.counted is True

    @pytest.mark.asyncio
    async def test_partial_recovery_when_already_counted_propagates(self):
        """If the original orphan was already counted (from settlement
        redirect), the first partial closure must carry already_counted=
        True so the coordinator does not bump."""
        pm = _make_pm()
        pm.adopt_orphan("KXBTC-PRECNT", "long", 5, 30.0,
                        cause="EXPIRY_409", counted=True)

        pm.client.get_market = AsyncMock(return_value={
            "market": {
                "ticker": "KXBTC-PRECNT",
                "status": "open",
                "yes_bid": 35,
            },
        })
        pm.client.create_order = AsyncMock(return_value={
            "order": {"order_id": "ord-recovery"},
        })
        pm.client.get_order = AsyncMock(return_value={
            "order": {"status": "executed", "fill_count_fp": "3",
                      "yes_price_dollars": "0.35"},
        })

        closed = await pm._check_orphans_inner()
        assert len(closed) == 1
        assert closed[0]["already_counted"] is True
        # Remainder also stays counted to prevent double-bump.
        assert pm.orphaned_positions[0].counted is True


# ══════════════════════════════════════════════════════════════════════
# Snapshot persistence — new fields round-trip cleanly
# ══════════════════════════════════════════════════════════════════════


class TestSnapshotRoundTrip:

    def test_snapshot_preserves_cause_and_counted(self):
        pm1 = _make_pm()
        pm1.adopt_orphan("KXBTC-RT", "long", 4, 25.0,
                         cause="EXPIRY_409", counted=True)
        snap = pm1.get_snapshot()

        pm2 = _make_pm()
        pm2.restore_from_snapshot(snap)
        assert len(pm2.orphaned_positions) == 1
        restored = pm2.orphaned_positions[0]
        assert restored.cause == "EXPIRY_409"
        assert restored.counted is True

    def test_snapshot_legacy_orphan_without_new_fields_restores(self):
        """Older bot-state snapshots (pre-cause/counted) must still
        deserialise cleanly — the new fields default to None/False."""
        pm = _make_pm()
        snap = {
            "state": "FLAT",
            "position": None,
            "orphaned_positions": [
                {
                    "ticker": "KXBTC-LEGACY",
                    "direction": "long",
                    "contracts": 3,
                    "avg_entry_price": 20.0,
                    "detected_at": "2026-04-19T00:00:00+00:00",
                },
            ],
            "completed_live_trades": 0,
            "live_trade_limit": 2,
            "settled_tickers": [],
        }
        pm.restore_from_snapshot(snap)
        assert len(pm.orphaned_positions) == 1
        assert pm.orphaned_positions[0].cause is None
        assert pm.orphaned_positions[0].counted is False

    def test_get_state_exposes_cause(self):
        pm = _make_pm()
        pm.adopt_orphan("KXBTC-DICT", "long", 2, 19.0, cause="EXPIRY_409")
        d = pm.get_state()
        assert d["orphaned_positions"][0]["cause"] == "EXPIRY_409"
