"""
Kalshi REST client extensions: historical markets, settlements,
public trades. Separate from kalshi_ws.py to keep concerns clean.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import AsyncIterator, Optional

import httpx
import structlog

from config import settings

logger = structlog.get_logger(__name__)

SERIES = "KXBTC"
PAGE_LIMIT = 1000
RATE_LIMIT_SLEEP = 0.12  # ~8 req/s, under Kalshi 10/s limit
DEFAULT_MAX_PAGES = 2000
MAX_RETRIES = 3


class KalshiHistoricalClient:
    """Thin async client for Kalshi historical + public endpoints."""

    def __init__(self):
        from data.kalshi_ws import KalshiAuth
        self._auth = KalshiAuth()
        self._base = settings.kalshi.base_url

    def _headers(self, method: str, path: str) -> dict:
        return self._auth.get_headers(method, path)

    async def _get(self, path: str, params: dict) -> dict:
        headers = self._headers("GET", "/trade-api/v2" + path)
        async with httpx.AsyncClient(
            base_url=self._base, timeout=30.0
        ) as c:
            r = await c.get(path, headers=headers, params=params)
            r.raise_for_status()
            return r.json()

    async def iter_historical_markets(
        self,
        series_ticker: str = SERIES,
        resume_cursor: Optional[str] = None,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> AsyncIterator[tuple[dict, str]]:
        """Paginate GET /historical/markets filtered to series_ticker.

        Yields (market_dict, cursor) tuples so the caller can persist
        the cursor for resumable sync.
        """
        cursor = resume_cursor
        page = 0
        while page < max_pages:
            params: dict = {
                "series_ticker": series_ticker,
                "limit": PAGE_LIMIT,
            }
            if cursor:
                params["cursor"] = cursor

            data = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    data = await self._get("/historical/markets", params)
                    break
                except Exception as e:
                    logger.warning("kalshi_hist.markets_fetch_retry",
                                   attempt=attempt, error=str(e))
                    if attempt == MAX_RETRIES:
                        logger.error("kalshi_hist.markets_fetch_failed",
                                     error=str(e), page=page)
                        return
                    await asyncio.sleep(2 ** attempt)

            next_cursor = data.get("cursor", "")
            for m in data.get("markets", []):
                yield m, next_cursor
            cursor = next_cursor
            if not cursor:
                break
            page += 1
            await asyncio.sleep(RATE_LIMIT_SLEEP)

        if page >= max_pages:
            logger.warning("kalshi_hist.markets_max_pages_reached", max_pages=max_pages)

    async def iter_historical_trades(
        self,
        ticker: Optional[str] = None,
        min_ts: Optional[datetime] = None,
        max_ts: Optional[datetime] = None,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> AsyncIterator[dict]:
        """Paginate GET /historical/trades for a specific KXBTC ticker."""
        cursor = None
        page = 0
        while page < max_pages:
            params: dict = {"limit": PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor
            if ticker:
                params["ticker"] = ticker
            if min_ts:
                params["min_ts"] = int(min_ts.timestamp())
            if max_ts:
                params["max_ts"] = int(max_ts.timestamp())

            data = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    data = await self._get("/historical/trades", params)
                    break
                except Exception as e:
                    logger.warning("kalshi_hist.trades_fetch_retry",
                                   attempt=attempt, error=str(e))
                    if attempt == MAX_RETRIES:
                        logger.error("kalshi_hist.trades_fetch_failed",
                                     error=str(e), page=page)
                        return
                    await asyncio.sleep(2 ** attempt)

            for t in data.get("trades", []):
                yield t
            cursor = data.get("cursor")
            if not cursor:
                break
            page += 1
            await asyncio.sleep(RATE_LIMIT_SLEEP)

        if page >= max_pages:
            logger.warning("kalshi_hist.trades_max_pages_reached", max_pages=max_pages)

    async def iter_live_trades(
        self,
        ticker: Optional[str] = None,
        min_ts: Optional[datetime] = None,
        max_ts: Optional[datetime] = None,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> AsyncIterator[dict]:
        """Paginate GET /markets/trades for active (non-archived) contracts."""
        cursor = None
        page = 0
        while page < max_pages:
            params: dict = {"limit": PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor
            if ticker:
                params["ticker"] = ticker
            if min_ts:
                params["min_ts"] = int(min_ts.timestamp())
            if max_ts:
                params["max_ts"] = int(max_ts.timestamp())

            data = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    data = await self._get("/markets/trades", params)
                    break
                except Exception as e:
                    logger.warning("kalshi_live.trades_fetch_retry",
                                   attempt=attempt, error=str(e))
                    if attempt == MAX_RETRIES:
                        logger.error("kalshi_live.trades_fetch_failed",
                                     error=str(e), page=page)
                        return
                    await asyncio.sleep(2 ** attempt)

            for t in data.get("trades", []):
                yield t
            cursor = data.get("cursor")
            if not cursor:
                break
            page += 1
            await asyncio.sleep(RATE_LIMIT_SLEEP)

        if page >= max_pages:
            logger.warning("kalshi_live.trades_max_pages_reached", max_pages=max_pages)

    async def get_active_tickers(self, series_ticker: str = SERIES) -> list[str]:
        """Fetch currently active (non-settled) KXBTC tickers from Kalshi API."""
        tickers = []
        try:
            data = await self._get("/markets", {
                "series_ticker": series_ticker,
                "status": "open",
                "limit": 200,
            })
            for m in data.get("markets", []):
                t = m.get("ticker", "")
                if "KXBTC" in t:
                    tickers.append(t)
        except Exception as e:
            logger.warning("kalshi_hist.active_tickers_failed", error=str(e))
        return tickers
