"""
Predexon API client for historical L2 orderbook snapshots.
Used exclusively for bootstrapping ob_snapshots on first startup.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import AsyncIterator, Optional

import httpx
import structlog

from config import settings

logger = structlog.get_logger(__name__)

PAGE_LIMIT = 200
RATE_SLEEP = 0.25  # 4 req/s conservative


class PredexonClient:
    """Async client for Predexon Kalshi orderbook history endpoint."""

    def __init__(self):
        self._key = settings.historical_sync.predexon_api_key
        self._base = settings.historical_sync.predexon_base_url

    def _headers(self) -> dict:
        return {"x-api-key": self._key, "Accept": "application/json"}

    async def iter_ob_snapshots(
        self,
        ticker: str,
        min_ts: Optional[datetime] = None,
        max_ts: Optional[datetime] = None,
    ) -> AsyncIterator[dict]:
        """Paginate /kalshi/orderbooks for a single ticker."""
        pagination_key = None
        params: dict = {"ticker": ticker, "limit": PAGE_LIMIT}
        if min_ts:
            params["min_ts"] = int(min_ts.timestamp())
        if max_ts:
            params["max_ts"] = int(max_ts.timestamp())

        async with httpx.AsyncClient(base_url=self._base, timeout=30.0) as c:
            while True:
                if pagination_key:
                    params["pagination_key"] = pagination_key
                try:
                    r = await c.get(
                        "/kalshi/orderbooks",
                        headers=self._headers(),
                        params=params,
                    )
                    r.raise_for_status()
                    data = r.json()
                except Exception as e:
                    logger.error("predexon.fetch_failed", ticker=ticker, error=str(e))
                    break
                for snap in data.get("snapshots", []):
                    yield snap
                pag = data.get("pagination", {})
                if not pag.get("has_more"):
                    break
                pagination_key = pag.get("pagination_key")
                await asyncio.sleep(RATE_SLEEP)

    @staticmethod
    def compute_obi(snap: dict) -> float:
        """Compute OBI from a Predexon snapshot dict."""
        bid_depth = snap.get("bid_depth", 0) or 0
        ask_depth = snap.get("ask_depth", 0) or 0
        total = bid_depth + ask_depth
        return bid_depth / total if total > 0 else 0.5
