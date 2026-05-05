"""Unit tests for BUG-025 wallet PnL reconciliation in Coordinator.

Validates that ``Coordinator._persist_trade`` correctly:
  - Fetches the post-exit wallet balance for live trades.
  - Computes ``wallet_pnl = wallet_post - wallet_at_entry`` and
    ``pnl_drift = abs(recorded_pnl - wallet_pnl)``.
  - Writes a drift quarantine row to ``errored_trades`` when drift
    exceeds the threshold, but still records the main ``trades`` row
    (so attribution counts the round-trip and operators can compare).
  - Skips reconciliation entirely for paper trades and for live trades
    closed via settlement (no exit cost to compare against).
  - Persists ``entry_cost_dollars``, ``exit_cost_dollars``,
    ``wallet_pnl``, ``pnl_drift`` and ``fill_source`` to the new
    ``trades`` columns added by migration 006.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coordinator import Coordinator


# ── Test doubles ─────────────────────────────────────────────────────


@dataclass
class _FakeTrade:
    """Minimal LiveTrade-shaped object for _persist_trade testing."""
    ticker: str = "KXBTC-T"
    direction: str = "long"
    contracts: int = 5
    entry_price: float = 30.0
    exit_price: float = 70.0
    pnl: float = 1.50
    pnl_pct: float = 0.20
    fees: float = 0.05
    exit_reason: str = "TEST"
    conviction: str = "NORMAL"
    regime_at_entry: str = "MEDIUM"
    candles_held: int = 3
    entry_time: datetime = datetime.now(timezone.utc)
    exit_time: datetime = datetime.now(timezone.utc)
    entry_order_id: Optional[str] = "ord-entry"
    exit_order_id: Optional[str] = "ord-exit"
    entry_obi: float = 0.0
    entry_roc: float = 0.0
    signal_driver: str = "OBI"
    entry_cost_dollars: Optional[float] = 1.50
    exit_cost_dollars: Optional[float] = 3.50
    entry_fill_source: str = "fill_ws"
    exit_fill_source: str = "fill_ws"
    wallet_at_entry: Optional[float] = 250.00


class _FakeRow:
    """Minimal asyncpg/psycopg row stand-in returning a fixed id."""

    def __init__(self, returning_id: Optional[int] = 999) -> None:
        self._id = returning_id

    async def fetchone(self):
        return (self._id,) if self._id is not None else None


class _FakeConn:
    """Captures every execute() so tests can inspect SQL + params."""

    def __init__(self, calls: List[Tuple[str, tuple]]) -> None:
        self.calls = calls

    async def execute(self, sql: str, params: tuple = ()):
        self.calls.append((sql, params))
        return _FakeRow()


class _FakePool:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, tuple]] = []

    @asynccontextmanager
    async def connection(self):
        yield _FakeConn(self.calls)


def _make_coordinator(*, wallet_post_dollars: float) -> Coordinator:
    """Build a minimal Coordinator with mocked dependencies.

    ``KalshiOrderClient`` is patched at import-time so the live trader
    constructor doesn't try to load real RSA keys.
    """
    with patch("execution.live_trader.KalshiOrderClient"), \
         patch("data.fill_stream.KalshiAuth"), \
         patch("execution.position_manager.KalshiOrderClient", create=True), \
         patch("notifications.get_notifier") as mock_notifier:
        mock_notifier.return_value = MagicMock(
            trade_quarantined=AsyncMock(),
            db_error=AsyncMock(),
            ws_disconnected=AsyncMock(),
        )
        coord = Coordinator()
    coord._pool = _FakePool()
    coord.live_trader = MagicMock()
    coord.live_trader.client = MagicMock()
    coord.live_trader.client.get_balance = AsyncMock(
        return_value={"balance": int(wallet_post_dollars * 100)},
    )
    coord.live_sizer = MagicMock()
    coord.live_sizer.reverse_trade = MagicMock()
    coord.paper_sizer = MagicMock()
    coord.paper_sizer.reverse_trade = MagicMock()
    coord._detect_rapid_fire = MagicMock(return_value=False)
    return coord


def _find_insert(calls: List[Tuple[str, tuple]], table: str) -> Optional[Tuple[str, tuple]]:
    """Find the first INSERT into the given table in captured calls."""
    needle = f"INSERT INTO {table}"
    for sql, params in calls:
        if needle in sql:
            return sql, params
    return None


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_live_trade_no_drift_records_trade_row():
    """wallet_post - wallet_pre matches recorded PnL → no quarantine,
    main trades row is written with reconciliation columns populated."""
    coord = _make_coordinator(wallet_post_dollars=251.50)  # +$1.50 == trade.pnl
    trade = _FakeTrade(pnl=1.50, wallet_at_entry=250.00)

    quarantined, trade_id = await coord._persist_trade(trade, mode="live")

    assert quarantined is False
    assert trade_id == 999
    insert = _find_insert(coord._pool.calls, "trades")
    errored = _find_insert(coord._pool.calls, "errored_trades")
    assert errored is None, "no quarantine expected when drift is 0"
    assert insert is not None
    sql, params = insert
    # Column ordering relied upon by analytics: keep this assertion
    # explicit so refactors can't silently move columns.
    assert "entry_cost_dollars" in sql
    assert "wallet_pnl" in sql
    assert "fill_source" in sql
    # entry_cost_dollars, exit_cost_dollars, wallet_pnl, pnl_drift,
    # fill_source, position_uid are the LAST six params (position_uid
    # added in migration 013_trade_position_uid.sql).
    entry_cost, exit_cost, wallet_pnl, pnl_drift, fill_source, _uid = params[-6:]
    assert entry_cost == pytest.approx(1.50)
    assert exit_cost == pytest.approx(3.50)
    assert wallet_pnl == pytest.approx(1.50)
    assert pnl_drift == pytest.approx(0.0, abs=1e-6)
    assert fill_source == "fill_ws"
    coord.live_sizer.reverse_trade.assert_not_called()


@pytest.mark.asyncio
async def test_persist_live_trade_drift_above_threshold_quarantines_but_records():
    """drift > $0.05 → drift quarantine row written, main trades row STILL
    written (so attribution sees the trade), sizer NOT reversed (the
    cost-based PnL is still our best estimate)."""
    coord = _make_coordinator(wallet_post_dollars=250.20)  # +$0.20 vs $1.50 recorded
    trade = _FakeTrade(pnl=1.50, wallet_at_entry=250.00)

    quarantined, trade_id = await coord._persist_trade(trade, mode="live")

    # Drift quarantine path: NOT considered "structural" quarantine, so
    # the function returns (False, trade_id) and writes both rows.
    assert quarantined is False
    assert trade_id == 999

    errored = _find_insert(coord._pool.calls, "errored_trades")
    assert errored is not None
    err_sql, err_params = errored
    # error_reason is the second-to-last parameter in the errored_trades
    # INSERT (last is trading_mode).
    assert "BUG-025: PnL drift" in err_params[-2]

    main = _find_insert(coord._pool.calls, "trades")
    assert main is not None
    _, main_params = main
    _, _, wallet_pnl, pnl_drift, _ = main_params[-6:-1]
    assert wallet_pnl == pytest.approx(0.20)
    assert pnl_drift == pytest.approx(1.30)

    # CRITICAL: drift quarantines do NOT reverse the sizer.
    coord.live_sizer.reverse_trade.assert_not_called()


@pytest.mark.asyncio
async def test_persist_live_trade_drift_below_threshold_no_quarantine():
    """$0.04 drift < $0.05 threshold → clean trade, no quarantine."""
    coord = _make_coordinator(wallet_post_dollars=251.46)  # +$1.46 vs $1.50 recorded
    trade = _FakeTrade(pnl=1.50, wallet_at_entry=250.00)

    quarantined, trade_id = await coord._persist_trade(trade, mode="live")

    assert quarantined is False
    assert trade_id == 999
    assert _find_insert(coord._pool.calls, "errored_trades") is None
    main = _find_insert(coord._pool.calls, "trades")
    assert main is not None
    _, params = main
    _, _, wallet_pnl, pnl_drift, _ = params[-6:-1]
    assert pnl_drift == pytest.approx(0.04, abs=1e-6)
    assert wallet_pnl == pytest.approx(1.46)


@pytest.mark.asyncio
async def test_persist_paper_trade_skips_reconciliation():
    """Paper trades never call get_balance (no live wallet) and never
    trip the drift quarantine even with a wildly-off recorded PnL."""
    coord = _make_coordinator(wallet_post_dollars=999.99)
    trade = _FakeTrade(pnl=10.0, wallet_at_entry=250.00,
                        exit_fill_source="order_response")

    quarantined, trade_id = await coord._persist_trade(trade, mode="paper")

    assert quarantined is False
    assert trade_id == 999
    coord.live_trader.client.get_balance.assert_not_called()
    main = _find_insert(coord._pool.calls, "trades")
    assert main is not None
    _, params = main
    _, _, wallet_pnl, pnl_drift, _ = params[-6:-1]
    # Reconciliation skipped → wallet_pnl/drift NULL
    assert wallet_pnl is None
    assert pnl_drift is None
    # And no quarantine row.
    assert _find_insert(coord._pool.calls, "errored_trades") is None


@pytest.mark.asyncio
async def test_persist_live_settlement_trade_skips_drift_quarantine():
    """Settlement-closed trades have no exit order so a drift comparison
    is structural, not behavioral. They must NOT be drift-quarantined
    even with large drift, but they must still record reconciliation
    columns (with fill_source falling back to entry leg)."""
    coord = _make_coordinator(wallet_post_dollars=255.00)
    trade = _FakeTrade(
        pnl=1.50,
        wallet_at_entry=250.00,
        exit_fill_source="settlement",
        entry_fill_source="fill_ws",
    )

    quarantined, trade_id = await coord._persist_trade(trade, mode="live")

    assert quarantined is False
    assert _find_insert(coord._pool.calls, "errored_trades") is None
    main = _find_insert(coord._pool.calls, "trades")
    assert main is not None
    _, params = main
    _, _, wallet_pnl, pnl_drift, fill_source = params[-6:-1]
    # Reconciliation IS computed (wallet check still runs)
    assert wallet_pnl == pytest.approx(5.00)
    assert pnl_drift == pytest.approx(3.50)
    # But fill_source falls back to entry leg, not "settlement"
    assert fill_source == "fill_ws"


@pytest.mark.asyncio
async def test_persist_live_trade_no_wallet_at_entry_skips_reconciliation():
    """Restored from a pre-BUG-025 snapshot → wallet_at_entry is None →
    reconciliation skipped silently."""
    coord = _make_coordinator(wallet_post_dollars=251.50)
    trade = _FakeTrade(pnl=1.50, wallet_at_entry=None)

    quarantined, trade_id = await coord._persist_trade(trade, mode="live")

    assert quarantined is False
    coord.live_trader.client.get_balance.assert_not_called()
    main = _find_insert(coord._pool.calls, "trades")
    assert main is not None
    _, params = main
    _, _, wallet_pnl, pnl_drift, _ = params[-6:-1]
    assert wallet_pnl is None
    assert pnl_drift is None


@pytest.mark.asyncio
async def test_persist_live_trade_balance_failure_does_not_block_persist():
    """If get_balance() raises, we log + skip reconciliation but still
    write the trade row -- never lose the trade just because the balance
    endpoint hiccupped."""
    coord = _make_coordinator(wallet_post_dollars=251.50)
    coord.live_trader.client.get_balance = AsyncMock(side_effect=RuntimeError("503"))
    trade = _FakeTrade(pnl=1.50, wallet_at_entry=250.00)

    quarantined, trade_id = await coord._persist_trade(trade, mode="live")

    assert quarantined is False
    assert trade_id == 999
    assert _find_insert(coord._pool.calls, "errored_trades") is None
    main = _find_insert(coord._pool.calls, "trades")
    assert main is not None
    _, params = main
    _, _, wallet_pnl, pnl_drift, _ = params[-6:-1]
    assert wallet_pnl is None
    assert pnl_drift is None


@pytest.mark.asyncio
async def test_persist_live_trade_rapid_fire_quarantine_still_skips_main_row():
    """Pre-existing structural quarantines (RAPID_FIRE_LOOP, INSTANT_STOP)
    keep their behavior: error row written, sizer reversed, main row
    suppressed -- regardless of drift."""
    coord = _make_coordinator(wallet_post_dollars=251.50)
    coord._detect_rapid_fire = MagicMock(return_value=True)
    trade = _FakeTrade(pnl=1.50, wallet_at_entry=250.00)

    quarantined, trade_id = await coord._persist_trade(trade, mode="live")

    assert quarantined is True
    assert trade_id is None
    assert _find_insert(coord._pool.calls, "errored_trades") is not None
    assert _find_insert(coord._pool.calls, "trades") is None
    coord.live_sizer.reverse_trade.assert_called_once_with(1.50)


@pytest.mark.asyncio
async def test_fill_source_uses_exit_leg_when_not_settlement():
    """When exit went through the WS, ``fill_source`` reflects the exit
    leg even if entry was order_response (more recent = more relevant)."""
    coord = _make_coordinator(wallet_post_dollars=251.50)
    trade = _FakeTrade(
        pnl=1.50, wallet_at_entry=250.00,
        entry_fill_source="order_response",
        exit_fill_source="fill_ws",
    )

    await coord._persist_trade(trade, mode="live")

    main = _find_insert(coord._pool.calls, "trades")
    assert main is not None
    _, params = main
    *_, fill_source = params[-6:-1]
    assert fill_source == "fill_ws"


@pytest.mark.asyncio
async def test_persist_trade_writes_position_uid_for_telemetry_join():
    """Migration 013 added ``trades.position_uid`` so the exit-intelligence
    promotion-readiness query can join trades to ``position_telemetry``
    by an exact key (the time-window join was zero-width because
    trades.timestamp == closed_at). Lock the contract: when the trade
    object carries a ``position_uid``, the INSERT must include it as the
    last param and the SQL must reference the column."""
    coord = _make_coordinator(wallet_post_dollars=251.50)
    trade = _FakeTrade(pnl=1.50, wallet_at_entry=250.00)
    trade.position_uid = "paper-deadbeefcafebabe"

    await coord._persist_trade(trade, mode="paper")

    main = _find_insert(coord._pool.calls, "trades")
    assert main is not None
    sql, params = main
    assert "position_uid" in sql, (
        "INSERT into trades must reference position_uid for the "
        "exit-intelligence promotion query to find the row"
    )
    assert params[-1] == "paper-deadbeefcafebabe", (
        "position_uid must be the LAST insert param (telemetry-join key)"
    )


@pytest.mark.asyncio
async def test_persist_trade_writes_null_position_uid_when_missing():
    """Legacy/orphan trades do not carry a position_uid. Empty strings
    must be normalized to NULL so the partial index on position_uid
    stays compact and the join-readiness gate doesn't false-positive
    on uid='' rows."""
    coord = _make_coordinator(wallet_post_dollars=251.50)
    trade = _FakeTrade(pnl=1.50, wallet_at_entry=250.00)
    trade.position_uid = ""

    await coord._persist_trade(trade, mode="paper")

    main = _find_insert(coord._pool.calls, "trades")
    assert main is not None
    _, params = main
    assert params[-1] is None
