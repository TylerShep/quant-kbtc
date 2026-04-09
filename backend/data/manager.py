"""
DataManager — orchestrates Kalshi WS + Spot WS feeds.
Merges incoming data into a unified MarketState per symbol.
Ported from kalshi-trading-bot with SortedDict book improvement.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import structlog
from sortedcontainers import SortedDict

from config import settings

logger = structlog.get_logger(__name__)


@dataclass
class OrderBookState:
    """Order book maintained with SortedDict for O(log n) insert/delete."""

    def __init__(self):
        self.bids: SortedDict = SortedDict(lambda x: -x)
        self.asks: SortedDict = SortedDict()

    @property
    def best_yes_bid(self) -> Optional[float]:
        return self.bids.peekitem(0)[0] if self.bids else None

    @property
    def best_yes_ask(self) -> Optional[float]:
        return self.asks.peekitem(0)[0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        b, a = self.best_yes_bid, self.best_yes_ask
        if b is not None and a is not None:
            return (b + a) / 2
        return None

    @property
    def spread(self) -> Optional[float]:
        b, a = self.best_yes_bid, self.best_yes_ask
        if b is not None and a is not None:
            return a - b
        return None

    def top_n_bids(self, n: int = 10) -> list[tuple]:
        return list(self.bids.items())[:n]

    def top_n_asks(self, n: int = 10) -> list[tuple]:
        return list(self.asks.items())[:n]

    def obi(self, depth: int = 10) -> float:
        bid_vol = sum(s for _, s in self.top_n_bids(depth))
        ask_vol = sum(s for _, s in self.top_n_asks(depth))
        total = bid_vol + ask_vol
        return bid_vol / total if total > 0 else 0.5

    def apply_snapshot(self, yes_rows: list, no_rows: list):
        self.bids.clear()
        self.asks.clear()
        for price, size in self._parse_rows(yes_rows):
            self.bids[price] = size
        for price, size in self._parse_rows(no_rows):
            self.asks[100 - price] = size

    def apply_delta(self, side: str, price_cents: int, delta: int):
        if side == "yes":
            book = self.bids
            key = price_cents
        else:
            book = self.asks
            key = 100 - price_cents

        current = book.get(key, 0)
        new_size = current + delta
        if new_size <= 0:
            book.pop(key, None)
        else:
            book[key] = new_size

    def apply_level(self, side: str, price_cents: int, size: int):
        if side == "yes":
            book = self.bids
            key = price_cents
        else:
            book = self.asks
            key = 100 - price_cents

        if size == 0:
            book.pop(key, None)
        else:
            book[key] = size

    @staticmethod
    def _parse_rows(rows) -> list[tuple[int, int]]:
        if not rows:
            return []
        out = []
        for e in rows:
            if not e or len(e) < 2:
                continue
            a, b = e[0], e[1]
            if isinstance(a, str) or (isinstance(a, float) and a <= 1.5):
                p = int(round(float(a) * 100))
            else:
                p = int(a)
            s = int(float(b)) if not isinstance(b, int) else b
            out.append((p, s))
        return out


@dataclass
class MarketState:
    symbol: str
    kalshi_ticker: Optional[str] = None
    spot_price: Optional[float] = None
    spot_bid: Optional[float] = None
    spot_ask: Optional[float] = None
    spot_volume_24h: Optional[float] = None
    order_book: OrderBookState = field(default_factory=OrderBookState)
    last_trade_price: Optional[float] = None
    last_trade_size: Optional[int] = None
    volume: Optional[int] = None
    expiry_time: Optional[datetime] = None
    time_remaining_sec: Optional[int] = None
    resolved: bool = False
    resolved_outcome: Optional[bool] = None
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def update_time_remaining(self):
        if self.expiry_time:
            delta = (self.expiry_time - datetime.now(timezone.utc)).total_seconds()
            self.time_remaining_sec = max(0, int(delta))


class DataManager:
    """
    Central hub for all market data.
    Maintains MarketState and fires callbacks on each update.
    """

    def __init__(self):
        self.states: Dict[str, MarketState] = {
            settings.bot.market: MarketState(symbol=settings.bot.market)
        }
        self._listeners: List[Callable[[str, MarketState], None]] = []
        self._kalshi_ws = None
        self._spot_ws = None
        self._tick_task: Optional[asyncio.Task] = None

    def add_listener(self, cb: Callable[[str, MarketState], None]):
        self._listeners.append(cb)

    async def start(self):
        logger.info("data_manager.starting", market=settings.bot.market)

        from data.spot_client import SpotPriceClient
        from data.kalshi_ws import KalshiWebSocketClient

        self._spot_ws = SpotPriceClient(
            markets=[settings.bot.market],
            on_price=self._on_spot_price,
        )
        await self._spot_ws.start()

        self._kalshi_ws = KalshiWebSocketClient(
            markets=[settings.bot.market],
            on_update=self._on_kalshi_update,
        )
        await self._kalshi_ws.start()

        self._tick_task = asyncio.create_task(self._tick_loop())

    async def stop(self):
        if self._spot_ws:
            await self._spot_ws.stop()
        if self._kalshi_ws:
            await self._kalshi_ws.stop()
        if self._tick_task:
            self._tick_task.cancel()

    def _on_spot_price(self, symbol: str, price: float, detail: dict):
        state = self.states.get(symbol)
        if state:
            state.spot_price = price
            state.spot_bid = detail.get("best_bid")
            state.spot_ask = detail.get("best_ask")
            state.spot_volume_24h = detail.get("volume_24h")
            state.last_updated = datetime.now(timezone.utc)
            self._notify(symbol, state)

    def _on_kalshi_update(self, symbol: str, update: dict):
        state = self.states.get(symbol)
        if not state:
            return

        update_type = update.get("type")
        data = update.get("data", {})

        if update_type in ("orderbook_snapshot", "orderbook_delta"):
            self._apply_orderbook(state, update_type, data)
        elif update_type == "lifecycle_settled":
            res = data.get("result")
            if res in ("yes", "no"):
                state.resolved = True
                state.resolved_outcome = res == "yes"
            mt = data.get("market_ticker")
            if mt:
                state.kalshi_ticker = mt
        elif update_type == "ticker":
            state.volume = data.get("volume", state.volume)
            if data.get("close_time"):
                try:
                    state.expiry_time = datetime.fromisoformat(
                        data["close_time"].replace("Z", "+00:00")
                    )
                except Exception:
                    pass
            tr = data.get("result")
            if tr in ("yes", "no"):
                state.resolved = True
                state.resolved_outcome = tr == "yes"
        elif update_type == "trade":
            state.last_trade_price = data.get("yes_price", state.last_trade_price)
            state.last_trade_size = data.get("count", state.last_trade_size)

        state.last_updated = datetime.now(timezone.utc)
        state.update_time_remaining()
        self._notify(symbol, state)

    def _apply_orderbook(self, state: MarketState, update_type: str, data: dict):
        if update_type == "orderbook_snapshot":
            y_rows = (
                data.get("yes_dollars_fp")
                or data.get("yes_dollars")
                or data.get("yes")
            )
            n_rows = (
                data.get("no_dollars_fp")
                or data.get("no_dollars")
                or data.get("no")
            )
            state.order_book.apply_snapshot(y_rows or [], n_rows or [])
        else:
            if "price_dollars" in data and "delta_fp" in data:
                raw_cents = int(round(float(data["price_dollars"]) * 100))
                delta_sz = int(float(data["delta_fp"]))
                side = data.get("side", "yes")
                state.order_book.apply_delta(side, raw_cents, delta_sz)
            else:
                for entry in data.get("yes", []):
                    price, size = int(entry[0]), int(entry[1])
                    state.order_book.apply_level("yes", price, size)
                for entry in data.get("no", []):
                    price, size = int(entry[0]), int(entry[1])
                    state.order_book.apply_level("no", price, size)

    def _notify(self, symbol: str, state: MarketState):
        if self._kalshi_ws:
            tk = self._kalshi_ws.active_tickers.get(symbol)
            if tk:
                state.kalshi_ticker = tk

        for cb in self._listeners:
            try:
                cb(symbol, state)
            except Exception as e:
                logger.error("data_manager.listener_error", error=str(e))
                import asyncio
                from notifications import get_notifier
                asyncio.create_task(get_notifier().unhandled_exception(
                    location="data_manager.listener",
                    error=str(e),
                ))

    async def _tick_loop(self):
        while True:
            await asyncio.sleep(1)
            for state in self.states.values():
                state.update_time_remaining()
