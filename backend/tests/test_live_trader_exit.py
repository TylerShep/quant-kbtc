"""Unit tests for LiveTrader exit verification via PositionManager.

Validates that exit() only records a trade when the exchange confirms
the position is actually gone, and that check_orphans recovery verifies
fills before recording closes.
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from execution.live_trader import LiveTrader
from execution.position_manager import (
    PositionManager, ManagedPosition, OrphanedPosition, PositionState,
    VERIFY_FAILED,
)
from risk.position_sizer import PositionSizer


def _make_trader(bankroll: float = 50_000.0) -> LiveTrader:
    sizer = PositionSizer(bankroll)
    with patch("execution.live_trader.KalshiOrderClient"):
        trader = LiveTrader(sizer)
    trader.client = MagicMock()
    trader.position_manager.client = trader.client
    return trader


def _set_position(trader: LiveTrader, ticker: str = "KXBTC-TEST",
                  direction: str = "long", contracts: int = 5,
                  entry_price: float = 30.0) -> ManagedPosition:
    pos = ManagedPosition(
        ticker=ticker, direction=direction, contracts=contracts,
        entry_price=entry_price,
        entry_time=datetime.now(timezone.utc).isoformat(),
        conviction="NORMAL", regime_at_entry="MEDIUM",
    )
    trader.position_manager.position = pos
    trader.position_manager.state = PositionState.OPEN
    return pos


# ── exit(): verify_position_on_exchange returns -1 (API error) ──────────

@pytest.mark.asyncio
async def test_exit_rejects_when_verify_fails_and_no_poll_fill():
    """VERIFY_FAILED (-1) with no poll fill count => exit rejected."""
    trader = _make_trader()
    _set_position(trader)
    initial_bankroll = trader.sizer.bankroll

    trader.client.create_order = AsyncMock(return_value={
        "order": {"order_id": "ord-1"}
    })
    trader.position_manager._poll_order_fill = AsyncMock(return_value={
        "status": "resting", "fill_count_fp": "0.00"
    })
    trader.position_manager.verify_position_on_exchange = AsyncMock(
        return_value=VERIFY_FAILED
    )

    trade = await trader.exit(30.0, "TEST")

    assert trade is None
    assert trader.position is not None
    assert trader.sizer.bankroll == initial_bankroll
    assert len(trader.trades) == 0


@pytest.mark.asyncio
async def test_exit_accepts_when_verify_fails_but_poll_confirmed_fill():
    """VERIFY_FAILED (-1) but poll shows filled => trust the poll."""
    trader = _make_trader()
    _set_position(trader, contracts=5, entry_price=30.0)

    trader.client.create_order = AsyncMock(return_value={
        "order": {"order_id": "ord-1"}
    })
    trader.position_manager._poll_order_fill = AsyncMock(return_value={
        "status": "executed", "fill_count_fp": "5.00",
        "no_price": 70,
    })
    trader.position_manager.verify_position_on_exchange = AsyncMock(
        return_value=VERIFY_FAILED
    )

    trade = await trader.exit(30.0, "TEST")

    assert trade is not None
    assert trade.contracts == 5
    assert trader.position is None


# ── exit(): verify returns 0 (position gone) ────────────────────────────

@pytest.mark.asyncio
async def test_exit_succeeds_when_exchange_confirms_position_gone():
    """Exchange shows 0 remaining => exit is real."""
    trader = _make_trader()
    _set_position(trader, contracts=5, entry_price=30.0)

    trader.client.create_order = AsyncMock(return_value={
        "order": {"order_id": "ord-1"}
    })
    trader.position_manager._poll_order_fill = AsyncMock(return_value={
        "status": "executed", "fill_count_fp": "5.00",
        "no_price": 70,
    })
    trader.position_manager.verify_position_on_exchange = AsyncMock(return_value=0)

    trade = await trader.exit(30.0, "TEST")

    assert trade is not None
    assert trade.contracts == 5
    assert trader.position is None


@pytest.mark.asyncio
async def test_exit_uses_exchange_count_when_poll_shows_zero_but_exchange_partial():
    """Poll shows 0 fill but exchange shows 2 of 5 remaining => 3 exited."""
    trader = _make_trader()
    _set_position(trader, contracts=5, entry_price=30.0)

    trader.client.create_order = AsyncMock(return_value={
        "order": {"order_id": "ord-1"}
    })
    trader.position_manager._poll_order_fill = AsyncMock(return_value={
        "status": "resting", "fill_count_fp": "0.00",
    })
    trader.position_manager.verify_position_on_exchange = AsyncMock(return_value=2)

    trade = await trader.exit(30.0, "TEST")

    assert trade is not None
    assert trade.contracts == 3
    assert trader.position is None
    assert len(trader.orphaned_positions) == 1
    assert trader.orphaned_positions[0].contracts == 2


# ── exit(): verify returns N (still fully open) ─────────────────────────

@pytest.mark.asyncio
async def test_exit_rejects_when_exchange_still_shows_full_position():
    """Exchange shows all 5 contracts still open => phantom exit prevented."""
    trader = _make_trader()
    _set_position(trader, contracts=5, entry_price=30.0)
    initial_bankroll = trader.sizer.bankroll

    trader.client.create_order = AsyncMock(return_value={
        "order": {"order_id": "ord-1"}
    })
    trader.position_manager._poll_order_fill = AsyncMock(return_value={
        "status": "executed", "fill_count_fp": "5.00",
        "no_price": 70,
    })
    trader.position_manager.verify_position_on_exchange = AsyncMock(return_value=5)

    trade = await trader.exit(30.0, "TEST")

    assert trade is None
    assert trader.position is not None
    assert trader.sizer.bankroll == initial_bankroll
    assert len(trader.trades) == 0


@pytest.mark.asyncio
async def test_exit_uses_exchange_count_over_poll_when_mismatch():
    """Poll says 5 filled but exchange shows 2 remaining => trust exchange (3 exited)."""
    trader = _make_trader()
    _set_position(trader, contracts=5, entry_price=30.0)

    trader.client.create_order = AsyncMock(return_value={
        "order": {"order_id": "ord-1"}
    })
    trader.position_manager._poll_order_fill = AsyncMock(return_value={
        "status": "executed", "fill_count_fp": "5.00",
        "no_price": 70,
    })
    trader.position_manager.verify_position_on_exchange = AsyncMock(return_value=2)

    trade = await trader.exit(30.0, "TEST")

    assert trade is not None
    assert trade.contracts == 3
    assert len(trader.orphaned_positions) == 1
    assert trader.orphaned_positions[0].contracts == 2


# ── exit(): order placement failure ─────────────────────────────────────

@pytest.mark.asyncio
async def test_exit_rejects_on_order_api_failure():
    """create_order throws => position preserved, no trade."""
    trader = _make_trader()
    _set_position(trader)

    trader.client.create_order = AsyncMock(side_effect=Exception("API down"))
    trader.position_manager._recover_order_after_failure = AsyncMock(return_value=None)

    trade = await trader.exit(30.0, "TEST")

    assert trade is None
    assert trader.position is not None


# ── check_orphans: recovery verifies fills ──────────────────────────────

@pytest.mark.asyncio
async def test_orphan_recovery_keeps_orphan_when_not_filled():
    """Recovery order placed but not filled => orphan stays in remaining."""
    trader = _make_trader()
    orphan = OrphanedPosition(
        ticker="KXBTC-ORPHAN", direction="long", contracts=3,
        avg_entry_price=25.0,
        detected_at=datetime.now(timezone.utc).isoformat(),
    )
    trader.orphaned_positions = [orphan]

    trader.client.get_market = AsyncMock(return_value={
        "market": {"status": "open", "yes_bid": 30, "no_bid": 70}
    })
    trader.client.create_order = AsyncMock(return_value={
        "order": {"order_id": "orphan-ord-1"}
    })
    trader.position_manager._poll_order_fill = AsyncMock(return_value={
        "status": "resting", "fill_count_fp": "0.00",
    })
    trader.position_manager.verify_position_on_exchange = AsyncMock(return_value=3)

    closed = await trader.check_orphans()

    assert len(closed) == 0
    assert len(trader.orphaned_positions) == 1
    assert trader.orphaned_positions[0].ticker == "KXBTC-ORPHAN"


@pytest.mark.asyncio
async def test_orphan_recovery_records_close_when_filled():
    """Recovery order filled => orphan removed, close recorded."""
    trader = _make_trader()
    orphan = OrphanedPosition(
        ticker="KXBTC-ORPHAN", direction="long", contracts=3,
        avg_entry_price=25.0,
        detected_at=datetime.now(timezone.utc).isoformat(),
    )
    trader.orphaned_positions = [orphan]

    trader.client.get_market = AsyncMock(return_value={
        "market": {"status": "open", "yes_bid": 30, "no_bid": 70}
    })
    trader.client.create_order = AsyncMock(return_value={
        "order": {"order_id": "orphan-ord-1"}
    })
    trader.position_manager._poll_order_fill = AsyncMock(return_value={
        "status": "executed", "fill_count_fp": "3.00",
        "no_price": 70,
    })

    closed = await trader.check_orphans()

    assert len(closed) == 1
    assert closed[0]["ticker"] == "KXBTC-ORPHAN"
    assert closed[0]["contracts"] == 3
    assert closed[0]["reason"] == "ORPHAN_RECOVERY"
    assert len(trader.orphaned_positions) == 0


@pytest.mark.asyncio
async def test_orphan_recovery_partial_fill_keeps_remainder():
    """Recovery order partially filled => close for filled, new orphan for remainder."""
    trader = _make_trader()
    orphan = OrphanedPosition(
        ticker="KXBTC-ORPHAN", direction="long", contracts=5,
        avg_entry_price=25.0,
        detected_at=datetime.now(timezone.utc).isoformat(),
    )
    trader.orphaned_positions = [orphan]

    trader.client.get_market = AsyncMock(return_value={
        "market": {"status": "open", "yes_bid": 30, "no_bid": 70}
    })
    trader.client.create_order = AsyncMock(return_value={
        "order": {"order_id": "orphan-ord-1"}
    })
    trader.position_manager._poll_order_fill = AsyncMock(return_value={
        "status": "executed", "fill_count_fp": "3.00",
        "no_price": 70,
    })

    closed = await trader.check_orphans()

    assert len(closed) == 1
    assert closed[0]["contracts"] == 3
    assert len(trader.orphaned_positions) == 1
    assert trader.orphaned_positions[0].contracts == 2


@pytest.mark.asyncio
async def test_orphan_recovery_verify_failed_keeps_orphan():
    """Recovery order + verify fails => keep orphan (don't assume closed)."""
    trader = _make_trader()
    orphan = OrphanedPosition(
        ticker="KXBTC-ORPHAN", direction="long", contracts=3,
        avg_entry_price=25.0,
        detected_at=datetime.now(timezone.utc).isoformat(),
    )
    trader.orphaned_positions = [orphan]

    trader.client.get_market = AsyncMock(return_value={
        "market": {"status": "open", "yes_bid": 30, "no_bid": 70}
    })
    trader.client.create_order = AsyncMock(return_value={
        "order": {"order_id": "orphan-ord-1"}
    })
    trader.position_manager._poll_order_fill = AsyncMock(return_value={
        "status": "resting", "fill_count_fp": "0.00",
    })
    trader.position_manager.verify_position_on_exchange = AsyncMock(
        return_value=VERIFY_FAILED
    )

    closed = await trader.check_orphans()

    assert len(closed) == 0
    assert len(trader.orphaned_positions) == 1


# ── PositionManager state machine tests ──────────────────────────────────

@pytest.mark.asyncio
async def test_position_manager_state_starts_flat():
    """PositionManager starts in FLAT state."""
    trader = _make_trader()
    assert trader.position_manager.state == PositionState.FLAT


@pytest.mark.asyncio
async def test_position_manager_blocks_entry_when_not_flat():
    """Cannot enter when state is not FLAT."""
    trader = _make_trader()
    trader.position_manager.state = PositionState.DESYNC

    trader.position_manager._check_flat_on_exchange = AsyncMock(return_value=True)
    trader.client.create_order = AsyncMock(return_value={
        "order": {"order_id": "ord-1"}
    })

    pos = await trader.position_manager.enter(
        ticker="KXBTC-TEST", direction="long", contracts=5,
        price=30.0, conviction="NORMAL", regime="MEDIUM",
    )
    assert pos is None


@pytest.mark.asyncio
async def test_position_manager_blocks_entry_when_orphans_exist():
    """Cannot enter when orphans are present."""
    trader = _make_trader()
    trader.position_manager.orphaned_positions = [
        OrphanedPosition(
            ticker="KXBTC-OLD", direction="long", contracts=3,
            avg_entry_price=25.0,
            detected_at=datetime.now(timezone.utc).isoformat(),
        )
    ]

    pos = await trader.position_manager.enter(
        ticker="KXBTC-TEST", direction="long", contracts=5,
        price=30.0, conviction="NORMAL", regime="MEDIUM",
    )
    assert pos is None


@pytest.mark.asyncio
async def test_position_manager_aborts_entry_when_exchange_not_flat():
    """Entry aborted when exchange shows existing position (DESYNC)."""
    trader = _make_trader()

    trader.position_manager._check_flat_on_exchange = AsyncMock(return_value=False)

    pos = await trader.position_manager.enter(
        ticker="KXBTC-TEST", direction="long", contracts=5,
        price=30.0, conviction="NORMAL", regime="MEDIUM",
    )
    assert pos is None
    assert trader.position_manager.state == PositionState.DESYNC


@pytest.mark.asyncio
async def test_position_manager_successful_entry_uses_exchange_count():
    """Entry uses verified exchange count, not requested count."""
    trader = _make_trader()

    trader.position_manager._check_flat_on_exchange = AsyncMock(return_value=True)
    trader.client.create_order = AsyncMock(return_value={
        "order": {"order_id": "ord-1"}
    })
    trader.position_manager._poll_order_fill = AsyncMock(return_value={
        "status": "executed", "fill_count_fp": "5.00",
    })
    trader.position_manager.verify_position_on_exchange = AsyncMock(return_value=4)
    trader.position_manager._persist_state = AsyncMock()

    pos = await trader.position_manager.enter(
        ticker="KXBTC-TEST", direction="long", contracts=5,
        price=30.0, conviction="NORMAL", regime="MEDIUM",
    )
    assert pos is not None
    assert pos.contracts == 4
    assert trader.position_manager.state == PositionState.OPEN


@pytest.mark.asyncio
async def test_position_manager_snapshot_roundtrip():
    """State persistence: snapshot and restore produces identical state."""
    trader = _make_trader()
    _set_position(trader, ticker="KXBTC-SNAP", contracts=10, entry_price=45.0)
    trader.position_manager.adopt_orphan("KXBTC-OLD", "long", 3, 25.0)

    snapshot = trader.position_manager.get_snapshot()

    trader2 = _make_trader()
    trader2.position_manager.restore_from_snapshot(snapshot)

    assert trader2.position_manager.state == PositionState.OPEN
    assert trader2.position_manager.position.ticker == "KXBTC-SNAP"
    assert trader2.position_manager.position.contracts == 10
    assert len(trader2.position_manager.orphaned_positions) == 1
    assert trader2.position_manager.orphaned_positions[0].ticker == "KXBTC-OLD"


# ── BUG-025: FillStream VWAP overrides + fill_source labelling ──────────


def _make_fill(order_id: str, *, count: int, yes_cents: float,
               side: str = "yes", action: str = "buy",
               fee_cents: float = 0.0):
    """Build a Fill matching the dataclass in data.fill_stream.

    Imported lazily so the symbol resolves under the test stub."""
    from data.fill_stream import Fill
    return Fill(
        trade_id=f"t-{order_id}-{count}-{yes_cents}",
        order_id=order_id,
        ticker="KXBTC-TEST",
        side=side,
        action=action,
        yes_price_cents=yes_cents,
        count=count,
        fee_cents=fee_cents,
        is_taker=True,
        received_at=0.0,
    )


def _make_trader_with_fill_stream():
    """Build a LiveTrader whose PositionManager has a stub FillStream."""
    from unittest.mock import patch as _patch
    sizer = PositionSizer(50_000.0)
    fs = MagicMock()
    fs.drain_for_order = AsyncMock(return_value=[])
    with _patch("execution.live_trader.KalshiOrderClient"):
        trader = LiveTrader(sizer, fill_stream=fs)
    trader.client = MagicMock()
    trader.position_manager.client = trader.client
    return trader, fs


@pytest.mark.asyncio
async def test_exit_uses_ws_vwap_when_fill_stream_returns_fills():
    """When the WS returns Fill events, exit_price/exit_cost/fees are
    overridden by the VWAP-derived values rather than the polled order's
    quoted price."""
    trader, fs = _make_trader_with_fill_stream()
    _set_position(trader, ticker="KXBTC-VWAP", contracts=5, entry_price=30.0)
    trader.position_manager.position.entry_cost_dollars = 1.50
    trader.position_manager.position.entry_fees_dollars = 0.04

    fs.drain_for_order = AsyncMock(return_value=[
        _make_fill("ord-X", count=2, yes_cents=70.0, side="yes",
                   action="sell", fee_cents=2.0),
        _make_fill("ord-X", count=3, yes_cents=80.0, side="yes",
                   action="sell", fee_cents=3.0),
    ])

    trader.client.create_order = AsyncMock(return_value={
        "order": {"order_id": "ord-X"}
    })
    trader.position_manager._poll_order_fill = AsyncMock(return_value={
        "status": "executed",
        "fill_count_fp": "5.00",
        "yes_price": 60,
        "taker_fill_cost_dollars": "3.00",
        "taker_fees_dollars": "0.10",
    })
    trader.position_manager.verify_position_on_exchange = AsyncMock(return_value=0)

    trade = await trader.exit(60.0, "TEST")

    assert trade is not None
    # VWAP = (2*70 + 3*80) / 5 = 380/5 = 76
    assert trade.exit_price == pytest.approx(76.0)
    # Cost = 2*0.70 + 3*0.80 = 1.40 + 2.40 = 3.80
    assert trade.exit_cost_dollars == pytest.approx(3.80)
    assert trade.exit_fill_source == "fill_ws"


@pytest.mark.asyncio
async def test_exit_falls_back_to_poll_when_fill_stream_empty():
    """No WS fills => exit_price/exit_cost from polled order; source =
    order_response."""
    trader, fs = _make_trader_with_fill_stream()
    _set_position(trader, ticker="KXBTC-FALLBACK", contracts=5, entry_price=30.0)
    trader.position_manager.position.entry_cost_dollars = 1.50
    trader.position_manager.position.entry_fees_dollars = 0.04

    fs.drain_for_order = AsyncMock(return_value=[])

    trader.client.create_order = AsyncMock(return_value={
        "order": {"order_id": "ord-Y"}
    })
    trader.position_manager._poll_order_fill = AsyncMock(return_value={
        "status": "executed",
        "fill_count_fp": "5.00",
        "yes_price": 60,
        "taker_fill_cost_dollars": "3.00",
        "taker_fees_dollars": "0.05",
    })
    trader.position_manager.verify_position_on_exchange = AsyncMock(return_value=0)

    trade = await trader.exit(60.0, "TEST")

    assert trade is not None
    assert trade.exit_price == pytest.approx(60.0)
    assert trade.exit_cost_dollars == pytest.approx(3.0)
    assert trade.exit_fill_source == "order_response"


@pytest.mark.asyncio
async def test_exit_marks_partial_when_fills_below_min_count():
    """If the WS returns < min_count fills, source = fill_ws_partial but
    the VWAP is still preferred (it's truth for what filled)."""
    trader, fs = _make_trader_with_fill_stream()
    _set_position(trader, ticker="KXBTC-PARTIAL", contracts=5, entry_price=30.0)
    trader.position_manager.position.entry_cost_dollars = 1.50
    trader.position_manager.position.entry_fees_dollars = 0.04

    fs.drain_for_order = AsyncMock(return_value=[
        _make_fill("ord-Z", count=2, yes_cents=70.0, side="yes",
                   action="sell", fee_cents=2.0),
    ])
    trader.client.create_order = AsyncMock(return_value={
        "order": {"order_id": "ord-Z"}
    })
    trader.position_manager._poll_order_fill = AsyncMock(return_value={
        "status": "executed", "fill_count_fp": "5.00", "yes_price": 60,
    })
    trader.position_manager.verify_position_on_exchange = AsyncMock(return_value=0)

    trade = await trader.exit(60.0, "TEST")

    assert trade is not None
    # 2 fills vs min_count=5 -> WS values still win (it's authoritative
    # for what *did* fill); only the source label differs.
    assert trade.exit_price == pytest.approx(70.0)
    assert trade.exit_fill_source == "fill_ws_partial"


@pytest.mark.asyncio
async def test_settlement_marks_exit_fill_source_as_settlement():
    """handle_settlement should label the trade's exit_fill_source as
    'settlement' since no Kalshi order was ever placed."""
    trader, fs = _make_trader_with_fill_stream()
    _set_position(trader, ticker="KXBTC-SETTLE", contracts=4, entry_price=30.0)

    trader.client.get_positions = AsyncMock(return_value={
        "market_positions": [
            {"ticker": "KXBTC-SETTLE", "position_fp": "0",
             "total_traded_dollars": "0"},
        ],
    })
    trader.position_manager._persist_state = AsyncMock()

    result = await trader.position_manager.handle_settlement("yes")
    assert result is not None
    assert result["exit_fill_source"] == "settlement"


# ── BUG-025: snapshot roundtrip carries new reconciliation fields ───────


@pytest.mark.asyncio
async def test_snapshot_roundtrip_preserves_fill_source_and_wallet():
    """ManagedPosition.entry_fill_source / wallet_at_entry must survive a
    snapshot/restore so a mid-position bot restart still has the
    reconciliation context when the exit eventually fires."""
    trader = _make_trader()
    pos = _set_position(trader, ticker="KXBTC-RECON", contracts=4, entry_price=40.0)
    pos.entry_cost_dollars = 1.60
    pos.entry_fees_dollars = 0.05
    pos.entry_fill_source = "fill_ws"
    pos.wallet_at_entry = 250.42

    snapshot = trader.position_manager.get_snapshot()

    trader2 = _make_trader()
    trader2.position_manager.restore_from_snapshot(snapshot)

    restored = trader2.position_manager.position
    assert restored is not None
    assert restored.entry_fill_source == "fill_ws"
    assert restored.wallet_at_entry == pytest.approx(250.42)
    assert restored.entry_cost_dollars == pytest.approx(1.60)


@pytest.mark.asyncio
async def test_snapshot_with_unknown_keys_is_filtered():
    """A snapshot saved by an older bot (or a future schema) with extra
    keys should restore cleanly rather than crashing the constructor."""
    trader = _make_trader()
    snapshot = {
        "state": "OPEN",
        "position": {
            "ticker": "KXBTC-OLD-SNAP",
            "direction": "long",
            "contracts": 3,
            "entry_price": 25.0,
            "entry_time": "2026-04-19T12:00:00+00:00",
            "conviction": "NORMAL",
            "regime_at_entry": "MEDIUM",
            "future_field_we_dont_know_about": "garbage",
        },
        "orphaned_positions": [],
        "exit_cooldowns": {},
        "completed_live_trades": 0,
    }
    trader.position_manager.restore_from_snapshot(snapshot)
    assert trader.position_manager.position is not None
    assert trader.position_manager.position.ticker == "KXBTC-OLD-SNAP"
    # Defaults applied for missing-but-known fields
    assert trader.position_manager.position.entry_fill_source == "order_response"
    assert trader.position_manager.position.wallet_at_entry is None
