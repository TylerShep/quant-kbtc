"""Tests for DB connection-pool sizing (BUG-029).

The default pool was ``min_size=2, max_size=10`` which saturated under
production load and caused ``psycopg_pool.PoolTimeout`` cascades that
appeared to drive container restarts.

These tests pin the new defaults and verify env overrides flow through.

We construct ``DatabaseConfig`` instances directly inside each test rather
than relying on ``importlib.reload``: dataclass defaults come from
``field(default_factory=lambda: _env(...))`` which evaluates on every
instance construction, so a freshly-built instance always reflects the
current env.
"""
from __future__ import annotations

import os

import pytest

from config.settings import DatabaseConfig


def test_default_pool_size_within_safe_bounds(monkeypatch):
    """Defaults must be high enough to avoid PoolTimeout under the bot's
    concurrent DB load, but low enough that two bot instances + admin
    overhead don't blow past Postgres's ``max_connections`` budget.

    Sized for two co-tenant bots (prod + canary) sharing a single Postgres
    with ``max_connections=100``: 2 * 20 + 5 admin + headroom = ~50 <= 100.
    """
    for var in (
        "DB_POOL_MIN_SIZE", "DB_POOL_MAX_SIZE", "DB_POOL_TIMEOUT_SEC",
        "DB_POOL_MAX_IDLE_SEC", "DB_POOL_MAX_LIFETIME_SEC",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = DatabaseConfig()
    assert cfg.pool_min_size >= 5
    assert 15 <= cfg.pool_max_size <= 30
    assert cfg.pool_timeout_sec <= 15.0


def test_pool_size_overridable_from_env(monkeypatch):
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "8")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "60")
    monkeypatch.setenv("DB_POOL_TIMEOUT_SEC", "5.5")
    cfg = DatabaseConfig()
    assert cfg.pool_min_size == 8
    assert cfg.pool_max_size == 60
    assert cfg.pool_timeout_sec == pytest.approx(5.5)


def test_pool_kwargs_includes_required_keys():
    """The _pool_kwargs() helper must pass every knob psycopg_pool needs."""
    from database.connection import _pool_kwargs
    kw = _pool_kwargs()
    assert "min_size" in kw
    assert "max_size" in kw
    assert "timeout" in kw
    assert "max_idle" in kw
    assert "max_lifetime" in kw
    assert kw["min_size"] >= 1
    assert kw["max_size"] >= kw["min_size"]


def test_pool_stats_empty_when_unopened():
    """pool_stats must NOT raise when the pool hasn't been opened yet."""
    from database.connection import pool_stats
    stats = pool_stats()
    assert isinstance(stats, dict)


def test_write_gate_returns_singleton_semaphore():
    """Repeated write_gate() calls return the same semaphore so we don't
    accidentally allocate parallel gates that defeat the throttle."""
    import asyncio as _asyncio
    import database.connection as dbc
    dbc._write_gate = None
    g1 = dbc.write_gate()
    g2 = dbc.write_gate()
    assert g1 is g2
    assert isinstance(g1, _asyncio.Semaphore)


def test_write_gate_throttles_concurrent_holders():
    """Sanity: exactly N holders fit in the gate; N+1 must wait."""
    import asyncio as _asyncio
    import database.connection as dbc
    from config import settings as cfg
    original = cfg.database.write_gate_size
    object.__setattr__(cfg.database, "write_gate_size", 2)
    dbc._write_gate = None
    gate = dbc.write_gate()
    assert gate._value == 2

    held: list[bool] = []
    blocked: list[bool] = []

    async def holder(idx: int, blocker_event: _asyncio.Event):
        async with gate:
            held.append(True)
            await blocker_event.wait()

    async def runner():
        block = _asyncio.Event()
        t1 = _asyncio.create_task(holder(1, block))
        t2 = _asyncio.create_task(holder(2, block))
        await _asyncio.sleep(0.05)
        assert len(held) == 2

        t3 = _asyncio.create_task(holder(3, block))
        await _asyncio.sleep(0.05)
        assert len(held) == 2, "third holder must wait at the gate"
        blocked.append(True)

        block.set()
        await _asyncio.gather(t1, t2, t3)
        assert len(held) == 3

    loop = _asyncio.new_event_loop()
    try:
        loop.run_until_complete(runner())
    finally:
        loop.close()
        _asyncio.set_event_loop(_asyncio.new_event_loop())
        dbc._write_gate = None
        object.__setattr__(cfg.database, "write_gate_size", original)
    assert blocked == [True]
