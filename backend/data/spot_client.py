"""
Coinbase Advanced Trade WebSocket — real-time BTC spot price.
Ported from kalshi-trading-bot.
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable, Dict, Optional

import websockets
import structlog

from config import settings

logger = structlog.get_logger(__name__)

COINBASE_PRODUCTS = {"BTC": "BTC-USD", "ETH": "ETH-USD"}


class SpotPriceClient:
    def __init__(self, markets: list[str], on_price: Callable[[str, float, dict], None]):
        self.markets = markets
        self.on_price = on_price
        self._running = False
        self._latest: Dict[str, float] = {}

    async def start(self):
        self._running = True
        asyncio.create_task(self._run_forever())
        logger.info("spot_client.started", markets=self.markets)

    async def stop(self):
        self._running = False

    def latest_price(self, symbol: str) -> Optional[float]:
        return self._latest.get(symbol.upper())

    async def _run_forever(self):
        attempt = 0
        while self._running:
            try:
                await self._connect()
                attempt = 0
            except Exception as e:
                attempt += 1
                wait = min(2**attempt, 60)
                logger.warning("spot_client.disconnected", error=str(e), retry_in=wait)
                if self._running:
                    await asyncio.sleep(wait)

    async def _connect(self):
        products = [COINBASE_PRODUCTS[m] for m in self.markets if m in COINBASE_PRODUCTS]
        if not products:
            return

        async with websockets.connect(
            settings.spot.coinbase_ws_url,
            ping_interval=20,
            ping_timeout=10,
            additional_headers={"User-Agent": "KBTC/1.0"},
        ) as ws:
            await ws.send(
                json.dumps(
                    {"type": "subscribe", "product_ids": products, "channel": "ticker"}
                )
            )
            logger.info("spot_client.subscribed", products=products)

            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw)
                    self._handle(msg)
                except json.JSONDecodeError:
                    pass

    def _handle(self, msg: dict):
        channel = msg.get("channel", "")
        if channel == "ticker":
            for event in msg.get("events", []):
                for ticker in event.get("tickers", []):
                    product_id = ticker.get("product_id", "")
                    symbol = next(
                        (s for s, p in COINBASE_PRODUCTS.items() if p == product_id),
                        None,
                    )
                    if symbol and ticker.get("price"):
                        price = float(ticker["price"])
                        detail = {
                            "price": price,
                            "volume_24h": float(ticker.get("volume_24_h", 0) or 0),
                            "best_bid": float(ticker.get("best_bid", price) or price),
                            "best_ask": float(ticker.get("best_ask", price) or price),
                        }
                        self._latest[symbol] = price
                        self.on_price(symbol, price, detail)

        elif msg.get("type") == "ticker":
            product_id = msg.get("product_id", "")
            symbol = next(
                (s for s, p in COINBASE_PRODUCTS.items() if p == product_id),
                None,
            )
            if symbol and msg.get("price"):
                price = float(msg["price"])
                detail = {
                    "price": price,
                    "best_bid": float(msg.get("best_bid", price) or price),
                    "best_ask": float(msg.get("best_ask", price) or price),
                }
                self._latest[symbol] = price
                self.on_price(symbol, price, detail)
