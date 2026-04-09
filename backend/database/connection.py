"""
Async connection pool using psycopg3.
"""
from __future__ import annotations

from psycopg_pool import AsyncConnectionPool
import structlog

from config import settings

logger = structlog.get_logger(__name__)

_pool: AsyncConnectionPool | None = None


async def get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            conninfo=settings.database.url,
            min_size=2,
            max_size=10,
            open=False,
        )
        await _pool.open()
        logger.info("db.pool_opened", url=settings.database.url.split("@")[-1])
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("db.pool_closed")
