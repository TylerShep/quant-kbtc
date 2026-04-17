"""
Kalshi historical markets proxy routes.

Thin pass-through to Kalshi's public (unauthenticated) REST endpoints that
power the Markets Historical dashboard tab. We proxy through the backend
rather than having the browser hit Kalshi directly so we can:
  - Avoid CORS / WAF fingerprint blocks from Kalshi's CDN
  - Cache responses server-side and smooth bursty tab-refresh traffic
  - Handle the live-vs-archived cutoff routing in one place

All endpoints are unauthenticated on Kalshi's side; no API key required.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query

from config import settings

router = APIRouter()


_CUTOFF_TTL = 300.0
_SETTLED_TTL = 60.0
_OPEN_TTL = 10.0
_CANDLES_LIVE_TTL = 15.0
_CANDLES_HIST_TTL = 600.0


class _TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, ttl: float) -> Any | None:
        entry = self._data.get(key)
        if entry and (time.monotonic() - entry[0]) < ttl:
            return entry[1]
        return None

    def set(self, key: str, value: Any) -> None:
        self._data[key] = (time.monotonic(), value)


_cache = _TTLCache()


def _kalshi_base() -> str:
    """Kalshi base URL including /trade-api/v2 prefix (from settings)."""
    return settings.kalshi.base_url


async def _kalshi_get(path: str, params: dict | None = None) -> dict:
    """Call Kalshi public REST; return parsed JSON or raise 502."""
    url = f"{_kalshi_base()}{path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url, params=params or {})
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Kalshi returned {e.response.status_code}: {e.response.text[:200]}",
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Kalshi upstream error: {e}")


def _iso_to_unix(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


@router.get("/historicals/cutoff")
async def historicals_cutoff() -> dict:
    """Return Kalshi's live-vs-historical cutoff timestamps.

    Kalshi returns ISO strings for market_settled_ts / trades_created_ts /
    orders_updated_ts. We also return unix-second variants for convenience
    so the frontend can do `expiration_unix < cutoff_unix` comparisons.
    """
    cached = _cache.get("cutoff", _CUTOFF_TTL)
    if cached is not None:
        return cached

    raw = await _kalshi_get("/historical/cutoff")

    result = {
        "raw": raw,
        "market_settled_ts": _iso_to_unix(raw.get("market_settled_ts")),
        "trades_created_ts": _iso_to_unix(raw.get("trades_created_ts")),
        "orders_updated_ts": _iso_to_unix(raw.get("orders_updated_ts")),
    }
    _cache.set("cutoff", result)
    return result


def _trim_market(m: dict) -> dict:
    return {
        "ticker": m.get("ticker"),
        "event_ticker": m.get("event_ticker"),
        "result": m.get("result"),
        "floor_strike": m.get("floor_strike"),
        "cap_strike": m.get("cap_strike"),
        "strike_type": m.get("strike_type"),
        "open_time": m.get("open_time"),
        "close_time": m.get("close_time"),
        "expiration_time": m.get("expiration_time"),
        "volume": m.get("volume_fp"),
        "volume_24h": m.get("volume_24h_fp"),
        "open_interest": m.get("open_interest_fp"),
        "settlement_value_dollars": m.get("settlement_value_dollars"),
        "last_price_dollars": m.get("last_price_dollars"),
    }


@router.get("/historicals/settled-markets")
async def historicals_settled_markets(
    series_ticker: str = Query("KXBTC"),
    limit: int = Query(24, ge=1, le=5000),
) -> dict:
    """Last N settled markets for the given series, newest first.

    Kalshi caps page size at ~200, so for larger limits we paginate via the
    cursor field on the underlying API, with a small delay between pages to
    stay under the public rate limit. Returns minimal subset of market
    metadata used by panels 1, 4, 5.

    On 429 we stop paginating and return what we have so far (the frontend
    uses whatever it gets; partial is better than an error).
    """
    cache_key = f"settled:{series_ticker}:{limit}"
    cached = _cache.get(cache_key, _SETTLED_TTL)
    if cached is not None:
        return cached

    page_size = 200
    remaining = limit
    cursor: str | None = None
    all_markets: list[dict] = []
    pages = 0
    rate_limited = False

    while remaining > 0 and pages < 30:
        params: dict[str, Any] = {
            "series_ticker": series_ticker,
            "status": "settled",
            "limit": min(page_size, remaining),
        }
        if cursor:
            params["cursor"] = cursor
        try:
            raw = await _kalshi_get("/markets", params=params)
        except HTTPException as e:
            if e.status_code == 502 and "429" in str(e.detail):
                rate_limited = True
                break
            raise
        page_markets = raw.get("markets", []) or []
        if not page_markets:
            break
        all_markets.extend(page_markets)
        remaining -= len(page_markets)
        cursor = raw.get("cursor")
        pages += 1
        if not cursor:
            break
        if pages < 30:
            await asyncio.sleep(0.25)

    trimmed = [_trim_market(m) for m in all_markets[:limit]]
    result = {
        "markets": trimmed,
        "count": len(trimmed),
        "pages_fetched": pages,
        "rate_limited": rate_limited,
    }
    _cache.set(cache_key, result)
    return result


@router.get("/historicals/current-market")
async def historicals_current_market(
    series_ticker: str = Query("KXBTC"),
) -> dict:
    """The currently-open market with the nearest close time.

    Used to identify which ticker to pull 1-min candles for in panels 2/3.
    Falls back to the most recently settled market if no market is open.
    """
    cache_key = f"current:{series_ticker}"
    cached = _cache.get(cache_key, _OPEN_TTL)
    if cached is not None:
        return cached

    open_raw = await _kalshi_get(
        "/markets",
        params={"series_ticker": series_ticker, "status": "open", "limit": 50},
    )
    open_markets = open_raw.get("markets", []) or []

    chosen = None
    if open_markets:
        open_markets.sort(key=lambda m: m.get("close_time") or "9999")
        chosen = open_markets[0]
        source = "open"
    else:
        recent = await _kalshi_get(
            "/markets",
            params={"series_ticker": series_ticker, "status": "settled", "limit": 1},
        )
        rows = recent.get("markets", []) or []
        if rows:
            chosen = rows[0]
            source = "most_recent_settled"
        else:
            source = "none"

    if chosen is None:
        result = {"market": None, "source": source}
    else:
        result = {
            "market": {
                "ticker": chosen.get("ticker"),
                "event_ticker": chosen.get("event_ticker"),
                "result": chosen.get("result"),
                "floor_strike": chosen.get("floor_strike"),
                "cap_strike": chosen.get("cap_strike"),
                "strike_type": chosen.get("strike_type"),
                "open_time": chosen.get("open_time"),
                "close_time": chosen.get("close_time"),
                "expiration_time": chosen.get("expiration_time"),
                "status": chosen.get("status"),
                "yes_bid_dollars": chosen.get("yes_bid_dollars"),
                "yes_ask_dollars": chosen.get("yes_ask_dollars"),
            },
            "source": source,
        }
    _cache.set(cache_key, result)
    return result


@router.get("/historicals/candlesticks")
async def historicals_candlesticks(
    ticker: str = Query(...),
    period_interval: int = Query(60, ge=1, le=1440),
    start_ts: int = Query(..., ge=0),
    end_ts: int = Query(..., ge=0),
    series_ticker: str = Query("KXBTC"),
    source: str | None = Query(
        None,
        description="'live' or 'historical'. If omitted, auto-routes based on end_ts vs cutoff.",
    ),
) -> dict:
    """Candlestick data for one market ticker.

    Routes to /series/{series}/markets/{ticker}/candlesticks for recent/live
    markets and /historical/markets/{ticker}/candlesticks for archived markets,
    using the cutoff endpoint. Caller can force `source=live|historical`.
    """
    if end_ts <= start_ts:
        raise HTTPException(status_code=400, detail="end_ts must be > start_ts")

    cache_key = (
        f"candles:{ticker}:{period_interval}:{start_ts}:{end_ts}:{source or 'auto'}"
    )

    if source == "historical":
        ttl = _CANDLES_HIST_TTL
    else:
        ttl = _CANDLES_LIVE_TTL
    cached = _cache.get(cache_key, ttl)
    if cached is not None:
        return cached

    use_historical = source == "historical"
    if source is None:
        cutoff_raw = await _kalshi_get("/historical/cutoff")
        cutoff_unix = _iso_to_unix(cutoff_raw.get("market_settled_ts"))
        if cutoff_unix is not None and end_ts < cutoff_unix:
            use_historical = True

    params = {
        "period_interval": period_interval,
        "start_ts": start_ts,
        "end_ts": end_ts,
    }

    if use_historical:
        path = f"/historical/markets/{ticker}/candlesticks"
    else:
        path = f"/series/{series_ticker}/markets/{ticker}/candlesticks"

    try:
        raw = await _kalshi_get(path, params=params)
    except HTTPException:
        if source is None and not use_historical:
            path = f"/historical/markets/{ticker}/candlesticks"
            raw = await _kalshi_get(path, params=params)
            use_historical = True
        else:
            raise

    result = {
        "ticker": ticker,
        "period_interval": period_interval,
        "source": "historical" if use_historical else "live",
        "candlesticks": raw.get("candlesticks", []),
    }
    _cache.set(cache_key, result)
    return result
