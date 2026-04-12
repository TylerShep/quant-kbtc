"""
Kalshi WebSocket client — order book deltas, ticker, trades, lifecycle.
Ported from kalshi-trading-bot with improvements.
"""
from __future__ import annotations

import asyncio
import json
import time
import base64
from pathlib import Path
from typing import Callable, Dict, Optional

import httpx
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import structlog

from config import settings

logger = structlog.get_logger(__name__)

MARKET_SERIES = {"BTC": "KXBTC", "ETH": "KXETH"}
COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/{product}/spot"


class KalshiAuth:
    def __init__(self):
        self._private_key = None
        key_path = Path(settings.kalshi.private_key_path)
        if key_path.exists():
            with open(key_path, "rb") as f:
                self._private_key = serialization.load_pem_private_key(
                    f.read(), password=None
                )
            logger.info("kalshi_auth.key_loaded")
        else:
            logger.warning("kalshi_auth.key_missing", path=str(key_path))

    def sign(self, message: str) -> str:
        if self._private_key is None:
            return "no-key"
        sig = self._private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def get_headers(self, method: str, path: str) -> Dict[str, str]:
        ts_ms = str(int(time.time() * 1000))
        msg = ts_ms + method.upper() + path
        return {
            "KALSHI-ACCESS-KEY": settings.kalshi.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": self.sign(msg),
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "Content-Type": "application/json",
        }


class KalshiRESTClient:
    def __init__(self):
        self.auth = KalshiAuth()
        self.base_url = settings.kalshi.base_url

    async def get_markets(self, series_ticker: str) -> dict:
        headers = self.auth.get_headers("GET", "/trade-api/v2/markets")
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as c:
            r = await c.get(
                "/markets",
                headers=headers,
                params={"series_ticker": series_ticker, "status": "open", "limit": 1000},
            )
            r.raise_for_status()
            return r.json()

    async def get_active_contract(self, symbol: str) -> Optional[dict]:
        series = MARKET_SERIES.get(symbol.upper())
        if not series:
            return None
        try:
            data = await self.get_markets(series)
            markets = data.get("markets", [])
            if not markets:
                return None

            spot = await self._fetch_spot(symbol)
            soonest = min(m.get("close_time", "9999") for m in markets)
            batch = [m for m in markets if m.get("close_time") == soonest]
            b_contracts = [m for m in batch if "-B" in m.get("ticker", "")]
            pool = b_contracts or batch
            if not pool:
                return None
            if spot is None:
                return pool[0]

            def dist(m):
                t = m.get("ticker", "")
                for sep in ("-B", "-T"):
                    if sep in t:
                        try:
                            return abs(float(t.split(sep)[-1].replace(",", "")) - spot)
                        except (ValueError, IndexError):
                            pass
                return 1e9

            return min(pool, key=dist)
        except Exception as e:
            logger.error("kalshi.get_active_contract_failed", error=str(e))
            return None

    @staticmethod
    async def _fetch_spot(symbol: str) -> Optional[float]:
        products = {"BTC": "BTC-USD", "ETH": "ETH-USD"}
        product = products.get(symbol.upper())
        if not product:
            return None
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(COINBASE_SPOT_URL.format(product=product))
                if r.status_code == 200:
                    return float(r.json()["data"]["amount"])
        except Exception:
            pass
        return None


class KalshiOrderClient:
    """Kalshi REST client for order placement and portfolio queries."""

    def __init__(self):
        self.auth = KalshiAuth()
        self.base_url = settings.kalshi.base_url

    async def create_order(
        self,
        ticker: str,
        side: str,
        action: str = "buy",
        count: int = 1,
        type: str = "market",
        yes_price: int | None = None,
        no_price: int | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        """Place an order on Kalshi. side: 'yes'|'no', type: 'market'|'limit'.

        client_order_id is a deduplication key — if the same ID is submitted
        multiple times, Kalshi recognises it as a duplicate and returns the
        existing order instead of creating a new one.
        """
        path = "/trade-api/v2/portfolio/orders"
        headers = self.auth.get_headers("POST", path)
        body: dict = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": type,
        }
        if client_order_id is not None:
            body["client_order_id"] = client_order_id
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price

        async with httpx.AsyncClient(base_url=self.base_url, timeout=15.0) as c:
            r = await c.post("/portfolio/orders", headers=headers, json=body)
            r.raise_for_status()
            return r.json()

    async def cancel_order(self, order_id: str) -> dict:
        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        headers = self.auth.get_headers("DELETE", path)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as c:
            r = await c.delete(f"/portfolio/orders/{order_id}", headers=headers)
            r.raise_for_status()
            return r.json()

    async def get_order(self, order_id: str) -> dict:
        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        headers = self.auth.get_headers("GET", path)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as c:
            r = await c.get(f"/portfolio/orders/{order_id}", headers=headers)
            r.raise_for_status()
            return r.json()

    async def get_positions(
        self,
        ticker: str | None = None,
        count_filter: str | None = None,
    ) -> dict:
        """Fetch portfolio positions. Use ticker to query a single market,
        count_filter='position' to return only non-zero positions."""
        path = "/trade-api/v2/portfolio/positions"
        headers = self.auth.get_headers("GET", path)
        params: dict = {}
        if ticker is not None:
            params["ticker"] = ticker
        if count_filter is not None:
            params["count_filter"] = count_filter
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as c:
            r = await c.get("/portfolio/positions", headers=headers, params=params or None)
            r.raise_for_status()
            return r.json()

    async def get_balance(self) -> dict:
        path = "/trade-api/v2/portfolio/balance"
        headers = self.auth.get_headers("GET", path)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as c:
            r = await c.get("/portfolio/balance", headers=headers)
            r.raise_for_status()
            return r.json()

    async def get_orders(self, status: str = "resting") -> dict:
        path = "/trade-api/v2/portfolio/orders"
        headers = self.auth.get_headers("GET", path)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as c:
            r = await c.get("/portfolio/orders", headers=headers,
                            params={"status": status, "limit": 100})
            r.raise_for_status()
            return r.json()

    async def get_market(self, ticker: str) -> dict:
        path = f"/trade-api/v2/markets/{ticker}"
        headers = self.auth.get_headers("GET", path)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as c:
            r = await c.get(f"/markets/{ticker}", headers=headers)
            r.raise_for_status()
            return r.json()


class KalshiWebSocketClient:
    def __init__(self, markets: list[str], on_update: Callable[[str, dict], None]):
        self.markets = markets
        self.on_update = on_update
        self.auth = KalshiAuth()
        self._running = False
        self._ws = None
        self.active_tickers: Dict[str, str] = {}
        self._active_close_times: Dict[str, str] = {}
        self.watched_position_tickers: Dict[str, str] = {}
        self._rest = KalshiRESTClient()
        self.connected = False
        self.last_message_time: Optional[float] = None
        self.message_count = 0
        self.connect_attempts = 0

    async def start(self):
        self._running = True
        await self._resolve_tickers()
        asyncio.create_task(self._run_forever())
        asyncio.create_task(self._refresh_loop())
        logger.info("kalshi_ws.started")

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _resolve_tickers(self):
        for symbol in self.markets:
            contract = await self._rest.get_active_contract(symbol)
            if contract:
                self.active_tickers[symbol] = contract["ticker"]
                self._active_close_times[symbol] = contract.get("close_time", "")
                logger.info(
                    "kalshi.ticker_resolved",
                    symbol=symbol,
                    ticker=contract["ticker"],
                )
            else:
                logger.warning("kalshi.no_active_contract", symbol=symbol)

    def _ws_is_open(self) -> bool:
        """Check if the WS connection is open (compatible with websockets 14.x)."""
        if self._ws is None:
            return False
        try:
            from websockets.protocol import State
            return self._ws.state is State.OPEN
        except Exception:
            return self.connected

    async def _refresh_loop(self):
        while self._running:
            await asyncio.sleep(60)
            if not self._running:
                break
            try:
                old = dict(self.active_tickers)
                await self._resolve_tickers()
                changed = {s for s, t in self.active_tickers.items() if old.get(s) != t}
                if changed and self._ws_is_open():
                    logger.info("kalshi_ws.ticker_changed", changed=list(changed))
                    await self._subscribe(self._ws)
            except Exception as e:
                logger.error("kalshi_ws.refresh_error", error=str(e))
                from notifications import get_notifier
                asyncio.create_task(get_notifier().unhandled_exception(
                    location="kalshi_ws._refresh_loop",
                    error=str(e),
                ))

    async def _run_forever(self):
        from notifications import get_notifier
        attempt = 0
        while self._running:
            try:
                await self._connect()
                attempt = 0
            except Exception as e:
                attempt += 1
                wait = min(2**attempt, 60)
                logger.warning("kalshi_ws.disconnected", error=str(e), retry_in=wait)
                if attempt >= 3:
                    asyncio.create_task(get_notifier().ws_disconnected(
                        feed="Kalshi",
                        error=str(e),
                        attempt=attempt,
                    ))
                if self._running:
                    await asyncio.sleep(wait)

    async def _connect(self):
        ts_ms = str(int(time.time() * 1000))
        signed_path = "/trade-api/ws/v2"
        sig = self.auth.sign(ts_ms + "GET" + signed_path)

        ws_headers = {
            "KALSHI-ACCESS-KEY": settings.kalshi.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        }

        self.connect_attempts += 1
        async with websockets.connect(
            settings.kalshi.ws_url,
            additional_headers=ws_headers,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            self._ws = ws
            self.connected = True
            await self._subscribe(ws)
            try:
                async for raw in ws:
                    if not self._running:
                        break
                    try:
                        msg = json.loads(raw)
                        self.last_message_time = time.time()
                        self.message_count += 1
                        self._handle_message(msg)
                    except json.JSONDecodeError:
                        pass
            finally:
                self.connected = False

    async def _subscribe(self, ws):
        tickers = list(self.active_tickers.values())
        if tickers:
            await ws.send(
                json.dumps(
                    {
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["orderbook_delta", "ticker", "trade"],
                            "market_tickers": tickers,
                        },
                    }
                )
            )
        await ws.send(
            json.dumps(
                {
                    "id": 2,
                    "cmd": "subscribe",
                    "params": {"channels": ["market_lifecycle_v2"]},
                }
            )
        )
        logger.info("kalshi_ws.subscribed", tickers=tickers)

    def _handle_message(self, msg: dict):
        msg_type = msg.get("type", "")

        if msg_type == "market_lifecycle_v2":
            m = msg.get("msg", {})
            et = m.get("event_type")
            if et in ("determined", "settled"):
                result = m.get("result")
                ticker = m.get("market_ticker")
                if result in ("yes", "no") and ticker:
                    active = ticker in self.active_tickers.values()
                    watched = ticker in self.watched_position_tickers
                    if active or watched:
                        symbol = self._ticker_to_symbol(ticker)
                        if not symbol and watched:
                            symbol = self.watched_position_tickers[ticker]
                        if symbol:
                            self.on_update(
                                symbol,
                                {"type": "lifecycle_settled", "data": {"result": result, "market_ticker": ticker}},
                            )
                            if active:
                                asyncio.create_task(self._on_contract_settled(symbol))
                    else:
                        logger.debug("kalshi_ws.lifecycle_ignored",
                                     ticker=ticker, event_type=et)
            return

        if msg_type in ("orderbook_snapshot", "orderbook_delta", "ticker", "trade"):
            ticker = msg.get("msg", {}).get("market_ticker", "")
            symbol = self._ticker_to_symbol(ticker) or ticker
            self.on_update(symbol, {"type": msg_type, "data": msg.get("msg", {})})

    async def _on_contract_settled(self, symbol: str):
        """Re-resolve ticker immediately when the active contract settles."""
        logger.info("kalshi_ws.contract_settled_reressolve", symbol=symbol)
        try:
            old = dict(self.active_tickers)
            await self._resolve_tickers()
            changed = {s for s, t in self.active_tickers.items() if old.get(s) != t}
            if changed and self._ws_is_open():
                logger.info("kalshi_ws.ticker_changed_post_settle", changed=list(changed))
                await self._subscribe(self._ws)
        except Exception as e:
            logger.error("kalshi_ws.settle_reressolve_failed", error=str(e))

    def _ticker_to_symbol(self, ticker: str) -> Optional[str]:
        for sym, tk in self.active_tickers.items():
            if tk == ticker:
                return sym
        for sym, prefix in MARKET_SERIES.items():
            if ticker.startswith(prefix):
                return sym
        return None
