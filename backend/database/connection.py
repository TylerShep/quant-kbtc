"""
Async connection pool using psycopg3.

Pool sizing rationale (BUG-029):
    The default ``min_size=2, max_size=10`` was insufficient under production
    load. The bot makes concurrent DB calls from many places per tick:
      * ``coordinator._persist_signal`` on every signal evaluation
      * ``coordinator._persist_equity`` on every minute boundary
      * ``coordinator._save_state`` every 5 minutes
      * ``api.routes`` for ``/api/status`` polled by the dashboard every ~1s
      * ``api.routes`` for ``/api/diagnostics`` and many other read endpoints
      * ``ml.feature_capture.save_features`` and ``label_trade`` post-exit
      * ``edge_profile_review.py`` and ``edge_profile_apply.py`` weekly cycles
      * ``monitoring.live_health`` cooldown reads/writes hourly
    Several of those run as background ``asyncio.create_task`` so they pile up
    behind a small pool. Once the pool is saturated, ``getconn`` blocks for
    the full ``timeout`` (default 30s) and every downstream caller throws
    ``PoolTimeout``. That manifests as ``Exception in ASGI application`` 500s
    on the dashboard and ``coordinator.persist_signal_failed`` errors in the
    bot, the cascade of which appears to drive container restarts.

    The new defaults (``min_size=5, max_size=20``) give comfortable headroom
    while keeping us well within Postgres ``max_connections=100`` even with
    a co-tenant canary bot. ``timeout`` is reduced from 30s to 10s so callers
    fail fast.

    Even the larger pool can be exhausted in a sub-second burst when the
    coordinator fans out 30+ ``asyncio.create_task(self._persist_*)`` calls
    in response to a single tick. To absorb that, we ALSO gate writes through
    an application-level ``asyncio.Semaphore`` (``write_gate``) that bounds
    in-flight write operations. When a burst overflows the gate, callers wait
    inside Python without consuming a real DB connection; once the gate is
    free, they grab a connection and complete normally. This prevents the
    cascade where 30 tasks each block on ``pool.connection()`` for the full
    10s timeout and time out together.

    All values are env-overridable in case load profile changes.
"""
from __future__ import annotations

import asyncio

from psycopg_pool import AsyncConnectionPool
import structlog

from config import settings

logger = structlog.get_logger(__name__)

_pool: AsyncConnectionPool | None = None
_write_gate: asyncio.Semaphore | None = None


def write_gate() -> asyncio.Semaphore:
    """Application-level concurrency limiter for fire-and-forget writes.

    Sized to ``DB_WRITE_GATE_SIZE`` (default 8). Background coordinator
    tasks (``_persist_snapshot``, ``_persist_signal``, ``_persist_equity``,
    ``_save_state``, ``_save_paper_position``) should ``async with
    write_gate():`` around their work. ``/api/*`` request handlers do
    NOT use the gate — interactive requests are bounded by uvicorn's
    own concurrency limit and starving them would hang the dashboard.
    """
    global _write_gate
    if _write_gate is None:
        _write_gate = asyncio.Semaphore(settings.database.write_gate_size)
    return _write_gate


def _pool_kwargs() -> dict:
    """Return pool configuration sourced from env vars with safe defaults."""
    cfg = settings.database
    return {
        "min_size": cfg.pool_min_size,
        "max_size": cfg.pool_max_size,
        "timeout": cfg.pool_timeout_sec,
        "max_idle": cfg.pool_max_idle_sec,
        "max_lifetime": cfg.pool_max_lifetime_sec,
    }


async def get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        kwargs = _pool_kwargs()
        _pool = AsyncConnectionPool(
            conninfo=settings.database.url,
            open=False,
            **kwargs,
        )
        await _pool.open()
        logger.info(
            "db.pool_opened",
            url=settings.database.url.split("@")[-1],
            **kwargs,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("db.pool_closed")


def pool_stats() -> dict:
    """Snapshot of pool utilization for diagnostics. Safe to call anytime.

    Returned keys mirror ``psycopg_pool.AsyncConnectionPool.get_stats()``
    plus a synthetic ``in_use`` for convenience. Returns empty dict when
    the pool hasn't been opened yet.
    """
    if _pool is None:
        return {}
    try:
        stats = _pool.get_stats()
    except Exception as e:
        return {"error": str(e)}
    in_use = max(int(stats.get("pool_size", 0)) - int(stats.get("pool_available", 0)), 0)
    return {**stats, "in_use": in_use}
