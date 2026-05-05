"""Tests for paper-position persistence across restarts (BUG-029).

Open paper positions used to live only in memory, so a container restart
during an open trade orphaned the "trade opened" Discord notification and
caused the position record never to be written. We now snapshot it to
``bot_state`` on entry, on every candle increment, and clear it on exit.

These tests use a fake asyncpg-style pool that records every executed SQL
statement so we can verify save/clear/restore wire correctly without a
real database.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from execution.paper_trader import PaperPosition


def _arun(coro):
    """Run a coroutine on a private loop without closing the global default.

    ``asyncio.run`` closes the event loop after each call, which on Python
    3.12 leaves no current loop and breaks subsequent tests in the same
    pytest session that call ``asyncio.get_event_loop()`` without making
    one first. We instead create+set+run+restore so test ordering is safe.
    """
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
    """Construct a Coordinator with auth/network classes stubbed.

    The Coordinator constructor instantiates LiveTrader/FillStream which
    in turn try to load the Kalshi RSA private key. Patch them out so
    the test can run without the real key file.
    """
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
    """Records every SQL execution and supports a single canned fetchone row."""

    def __init__(self, store: dict):
        self._store = store
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, params: tuple = ()) -> _FakeCursor:
        self.executed.append((sql, params))
        sql_lower = sql.lower()
        if "select value from bot_state" in sql_lower and "paper_open_position" in sql_lower:
            row = self._store.get("paper_open_position")
            return _FakeCursor((row,) if row is not None else None)
        if "delete from bot_state" in sql_lower and "paper_open_position" in sql_lower:
            self._store.pop("paper_open_position", None)
            return _FakeCursor()
        if "insert into bot_state" in sql_lower and params:
            try:
                payload = json.loads(params[0])
                if "ticker" in payload:
                    self._store["paper_open_position"] = payload
            except (TypeError, ValueError):
                pass
            return _FakeCursor()
        return _FakeCursor()


class _FakePool:
    def __init__(self):
        self.store: dict = {}
        self.last_conn: _FakeConn | None = None

    def connection(self):
        pool = self

        class _Ctx:
            async def __aenter__(self_inner):
                pool.last_conn = _FakeConn(pool.store)
                return pool.last_conn

            async def __aexit__(self_inner, *a):
                return False

        return _Ctx()


def _coordinator_with_pool():
    coord = _make_coordinator()
    pool = _FakePool()
    coord._pool = pool
    return coord, pool


def _set_paper_position(coord: Coordinator) -> PaperPosition:
    pos = PaperPosition(
        ticker="KXBTC-26TEST-T100000",
        direction="long",
        contracts=4,
        entry_price=42.0,
        entry_time=datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc),
        conviction="HIGH",
        regime_at_entry="MEDIUM",
        entry_obi=0.123,
        entry_roc=0.045,
        candles_held=2,
        max_favorable_excursion=0.015,
        max_adverse_excursion=-0.008,
        signal_driver="OBI+ROC",
        position_uid="paper-test-uid-123",
    )
    coord.paper_trader.position = pos
    return pos


def test_save_paper_position_writes_to_bot_state():
    coord, pool = _coordinator_with_pool()
    pos = _set_paper_position(coord)
    _arun(coord._save_paper_position())
    assert "paper_open_position" in pool.store
    payload = pool.store["paper_open_position"]
    assert payload["ticker"] == pos.ticker
    assert payload["direction"] == "long"
    assert payload["contracts"] == 4
    assert payload["entry_price"] == 42.0
    assert payload["candles_held"] == 2
    assert payload["signal_driver"] == "OBI+ROC"
    assert payload["position_uid"] == "paper-test-uid-123"
    assert payload["entry_time"].startswith("2026-04-30T12:00")


def test_save_paper_position_no_op_when_no_position():
    coord, pool = _coordinator_with_pool()
    coord.paper_trader.position = None
    _arun(coord._save_paper_position())
    assert pool.store == {}


def test_clear_paper_position_removes_row():
    coord, pool = _coordinator_with_pool()
    pool.store["paper_open_position"] = {"ticker": "stale"}
    _arun(coord._clear_paper_position())
    assert "paper_open_position" not in pool.store


def test_restore_paper_position_round_trip():
    """Save -> simulate restart -> restore -> trader has same position."""
    coord_a, pool = _coordinator_with_pool()
    original = _set_paper_position(coord_a)
    _arun(coord_a._save_paper_position())

    coord_b = _make_coordinator()
    coord_b._pool = pool
    assert coord_b.paper_trader.position is None
    _arun(coord_b._restore_paper_position())

    restored = coord_b.paper_trader.position
    assert restored is not None
    assert restored.ticker == original.ticker
    assert restored.direction == original.direction
    assert restored.contracts == original.contracts
    assert restored.entry_price == original.entry_price
    assert restored.candles_held == original.candles_held
    assert restored.conviction == original.conviction
    assert restored.regime_at_entry == original.regime_at_entry
    assert restored.signal_driver == original.signal_driver
    assert restored.position_uid == original.position_uid


def test_restore_paper_position_no_op_when_empty_store():
    coord, _pool = _coordinator_with_pool()
    _arun(coord._restore_paper_position())
    assert coord.paper_trader.position is None


def test_restore_paper_position_handles_corrupt_json():
    """Don't crash startup just because the persisted row was bad."""
    coord, pool = _coordinator_with_pool()
    pool.store["paper_open_position"] = "not-a-dict"
    _arun(coord._restore_paper_position())
    assert coord.paper_trader.position is None


def test_save_paper_position_swallows_pool_errors():
    """Persisting state must never crash the bot."""
    coord, _ = _coordinator_with_pool()
    _set_paper_position(coord)

    class _BrokenPool:
        def connection(self):
            raise RuntimeError("pool exhausted")

    coord._pool = _BrokenPool()
    _arun(coord._save_paper_position())
