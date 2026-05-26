"""
Predexon API client for historical L2 orderbook snapshots.
Used exclusively for bootstrapping ob_snapshots on first startup.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import AsyncIterator, Optional

import httpx
import structlog

from config import settings

logger = structlog.get_logger(__name__)

PAGE_LIMIT = 200
RATE_SLEEP = 0.25  # 4 req/s conservative

# Module-level dedup cache for predexon.fetch_failed logs. See call site
# below in iter_ob_snapshots for rationale.
_PREDEXON_LOG_DEDUP: dict = {}


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
                    # 2026-05-06 (BUG-035 follow-up): rate-limit to once
                    # per (ticker, error_class) per 5 min. Pre-fix, when
                    # Predexon's API returned 429s for many stale tickers,
                    # this fired ~141 times in 5 minutes (one per stale
                    # ticker per sync iteration), each line carrying the
                    # full URL + MDN docs string. Combined with structlog
                    # JSON encoding overhead per call, it noticeably
                    # added to the event-loop pressure that the BUG-035
                    # incident investigation was already chasing.
                    err_class = type(e).__name__
                    err_str = str(e)
                    # Cheap status-code bucket so 429 vs 422 are dedup'd
                    # separately even for the same ticker.
                    if "429" in err_str:
                        err_class += ":429"
                    elif "422" in err_str:
                        err_class += ":422"
                    elif "401" in err_str or "403" in err_str:
                        err_class += ":auth"
                    log_key = (ticker, err_class)
                    last = _PREDEXON_LOG_DEDUP.get(log_key)
                    now_t = time.time()
                    if last is None or (now_t - last) > 300.0:
                        _PREDEXON_LOG_DEDUP[log_key] = now_t
                        logger.error("predexon.fetch_failed", ticker=ticker, error=err_str)
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
