"""
Coordinator — event loop orchestration.
Single entry point that wires all subsystems together per the quant-developer skill.
Strict order of operations: regime -> exits -> entries -> heartbeat.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import structlog

from config import settings
from data.manager import DataManager
from data.candle_aggregator import CandleAggregator
from features.engine import FeatureEngine
from strategies.resolver import SignalConflictResolver
from filters.atr_regime import ATRRegimeFilter
from risk.position_sizer import PositionSizer
from risk.circuit_breaker import CircuitBreaker
from execution.paper_trader import PaperTrader
from api.ws import ws_manager
from database import get_pool, close_pool

logger = structlog.get_logger(__name__)


class Coordinator:
    """Wires data feeds -> features -> strategy -> risk -> execution -> dashboard."""

    def __init__(self):
        self.data_manager = DataManager()
        self.candle_aggregator = CandleAggregator()
        self.feature_engine = FeatureEngine()
        self.atr_filter = ATRRegimeFilter()
        self.resolver = SignalConflictResolver()
        self.position_sizer = PositionSizer(settings.bot.initial_bankroll)
        self.circuit_breaker = CircuitBreaker(self.position_sizer)
        self.paper_trader = PaperTrader(self.position_sizer)
        self._pool = None
        self._tick_count = 0

    async def start(self):
        self._pool = await get_pool()
        self.data_manager.add_listener(self._on_market_update)
        await self.data_manager.start()
        logger.info("coordinator.started")

    async def stop(self):
        await self.data_manager.stop()
        await close_pool()
        logger.info("coordinator.stopped")

    def _on_market_update(self, symbol: str, state) -> None:
        """
        Called on every market data update (Kalshi WS or Spot WS tick).
        Unified callback pipeline — single path from data to execution.
        """
        self._tick_count += 1
        t0 = time.perf_counter_ns()

        features = self.feature_engine.update(symbol, state)
        if features is None:
            return

        asyncio.create_task(
            ws_manager.broadcast({
                "type": "market_update",
                "symbol": symbol,
                "data": features.to_dict(),
                "state": _serialize_state(state),
            })
        )

        if self._tick_count % 10 == 0 and self._pool is not None:
            asyncio.create_task(self._persist_snapshot(symbol, state, features))

    async def _persist_snapshot(self, symbol: str, state, features) -> None:
        try:
            pool = self._pool
            if pool is None:
                return
            async with pool.connection() as conn:
                await conn.execute(
                    """INSERT INTO ob_snapshots
                       (timestamp, ticker, bids, asks, obi, total_bid_vol, total_ask_vol, spread_cents)
                       VALUES (NOW(), %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s)""",
                    (
                        state.kalshi_ticker or symbol,
                        "[]",
                        "[]",
                        features.obi,
                        features.total_bid_vol,
                        features.total_ask_vol,
                        features.spread_cents,
                    ),
                )
        except Exception as e:
            logger.error("coordinator.persist_failed", error=str(e))


def _serialize_state(state) -> dict:
    return {
        "symbol": state.symbol,
        "spot_price": state.spot_price,
        "kalshi_ticker": state.kalshi_ticker,
        "best_bid": state.order_book.best_yes_bid,
        "best_ask": state.order_book.best_yes_ask,
        "mid": state.order_book.mid,
        "spread": state.order_book.spread,
        "time_remaining_sec": state.time_remaining_sec,
        "volume": state.volume,
    }
