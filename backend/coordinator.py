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
from strategies.obi import evaluate_obi, check_obi_exit, Direction
from strategies.roc import evaluate_roc, calculate_roc, check_roc_exit
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
        self._last_decision = None
        self._last_exit_tick = -999

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

        features = self.feature_engine.update(symbol, state)
        if features is None:
            return

        # 1. Feed candle aggregator with spot ticks
        completed_candle = None
        if state.spot_price:
            completed_candle = self.candle_aggregator.on_tick(
                time.time(), state.spot_price
            )

        # 2. On candle close: update ATR regime
        if completed_candle:
            self.atr_filter.update(
                completed_candle.high,
                completed_candle.low,
                completed_candle.close,
            )
            if self.paper_trader.has_position:
                self.paper_trader.position.candles_held += 1

            logger.info(
                "candle.closed",
                o=round(completed_candle.open, 2),
                h=round(completed_candle.high, 2),
                l=round(completed_candle.low, 2),
                c=round(completed_candle.close, 2),
                regime=self.atr_filter.current_regime,
            )

        # 3. Check exits on every tick (not just candle close)
        regime = self.atr_filter.current_regime
        if self.paper_trader.has_position:
            exit_reason = self._check_exits(state, features, regime)
            if exit_reason:
                exit_price = self._get_exit_price(state)
                if exit_price is not None:
                    trade = self.paper_trader.exit(exit_price, exit_reason)
                    if trade:
                        self._last_exit_tick = self._tick_count
                        asyncio.create_task(self._persist_trade(trade))
                        asyncio.create_task(ws_manager.broadcast({
                            "type": "trade_exit",
                            "symbol": symbol,
                            "trade": {
                                "ticker": trade.ticker,
                                "direction": trade.direction,
                                "pnl": trade.pnl,
                                "exit_reason": trade.exit_reason,
                            },
                        }))

        # 4. Evaluate entry signals — cooldown of 30 ticks after an exit
        if not self.paper_trader.has_position:
            ticks_since_exit = self._tick_count - self._last_exit_tick
            if ticks_since_exit > 30:
                self._evaluate_entry(symbol, state, features, regime)

        # 5. Broadcast to dashboard
        asyncio.create_task(
            ws_manager.broadcast({
                "type": "market_update",
                "symbol": symbol,
                "data": features.to_dict(),
                "state": _serialize_state(state),
                "decision": self._serialize_decision(),
            })
        )

        # 6. Persist OB snapshots periodically
        if self._tick_count % 10 == 0 and self._pool is not None:
            asyncio.create_task(self._persist_snapshot(symbol, state, features))

    def _evaluate_entry(self, symbol: str, state, features, regime: str) -> None:
        can_trade, halt_reason = self.circuit_breaker.can_trade()

        obi_history = self.feature_engine.obi_history(symbol)
        total_vol = features.total_bid_vol + features.total_ask_vol

        obi_dir = evaluate_obi(
            obi_history=obi_history,
            total_book_volume=total_vol,
            atr_regime=regime,
            has_position=False,
        )

        candle_list = [
            {"open": c.open, "high": c.high, "low": c.low, "close": c.close}
            for c in self.candle_aggregator.recent(10)
        ]
        closes = [c.close for c in self.candle_aggregator.recent(10)]

        roc_dir = evaluate_roc(
            closes=closes,
            candles=candle_list,
            atr_regime=regime,
            obi_direction=obi_dir,
            has_position=False,
        )

        decision = self.resolver.resolve(
            obi_direction=obi_dir,
            roc_direction=roc_dir,
            atr_regime=regime,
            can_trade=can_trade,
        )
        self._last_decision = decision

        if decision.should_trade:
            entry_price = self._get_entry_price(state, decision.direction)
            if entry_price is not None and entry_price > 0:
                ticker = state.kalshi_ticker or symbol
                roc_val = calculate_roc(closes, settings.roc.lookback) or 0.0

                pos = self.paper_trader.enter(
                    ticker=ticker,
                    direction=decision.direction.value,
                    price=entry_price,
                    conviction=decision.conviction.value,
                    regime=regime,
                    obi=features.obi,
                    roc=roc_val,
                )
                if pos:
                    asyncio.create_task(self._persist_signal(
                        state, features, decision, "ENTRY"
                    ))
                    asyncio.create_task(ws_manager.broadcast({
                        "type": "trade_entry",
                        "symbol": symbol,
                        "position": {
                            "ticker": pos.ticker,
                            "direction": pos.direction,
                            "contracts": pos.contracts,
                            "entry_price": pos.entry_price,
                            "conviction": pos.conviction,
                        },
                    }))
        elif decision.skip_reason and self._tick_count % 60 == 0:
            asyncio.create_task(self._persist_signal(
                state, features, decision, decision.skip_reason
            ))

    def _check_exits(self, state, features, regime: str) -> Optional[str]:
        pos = self.paper_trader.position
        if pos is None:
            return None

        current_price = self._get_exit_price(state)
        if current_price is None:
            return None

        d = 1 if pos.direction == "long" else -1
        pnl_per_contract = d * (current_price - pos.entry_price) / 100
        notional = pos.contracts * pos.entry_price / 100
        pnl_pct = (pnl_per_contract * pos.contracts) / notional if notional > 0 else 0

        exit_reason = check_obi_exit(
            direction=pos.direction,
            current_obi=features.obi,
            pnl_pct=pnl_pct,
            candles_held=pos.candles_held,
            atr_regime=regime,
        )
        if exit_reason:
            return exit_reason

        closes = [c.close for c in self.candle_aggregator.recent(10)]
        current_roc = calculate_roc(closes, settings.roc.lookback)
        candle_list = self.candle_aggregator.recent(1)
        latest_candle = None
        if candle_list:
            c = candle_list[0]
            latest_candle = {"open": c.open, "high": c.high, "low": c.low, "close": c.close}

        exit_reason = check_roc_exit(
            direction=pos.direction,
            pnl_pct=pnl_pct,
            entry_roc=pos.entry_roc,
            current_roc=current_roc,
            latest_candle=latest_candle,
            candles_held=pos.candles_held,
        )
        return exit_reason

    def _get_entry_price(self, state, direction) -> Optional[float]:
        """Get entry price: buy YES at ask for LONG, buy NO (sell YES at bid) for SHORT."""
        if direction == Direction.LONG:
            return state.order_book.best_yes_ask
        else:
            return state.order_book.best_yes_bid

    def _get_exit_price(self, state) -> Optional[float]:
        """Get exit price based on current position direction."""
        pos = self.paper_trader.position
        if pos is None:
            return None
        mid = state.order_book.mid
        if mid is not None:
            return mid
        if pos.direction == "long":
            return state.order_book.best_yes_bid
        return state.order_book.best_yes_ask

    def _serialize_decision(self) -> Optional[dict]:
        d = self._last_decision
        if d is None:
            return None
        return {
            "direction": d.direction.value if d.direction else None,
            "conviction": d.conviction.value,
            "obi_dir": d.obi_dir.value,
            "roc_dir": d.roc_dir.value,
            "skip_reason": d.skip_reason,
            "should_trade": d.should_trade,
        }

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

    async def _persist_trade(self, trade) -> None:
        try:
            pool = self._pool
            if pool is None:
                return
            async with pool.connection() as conn:
                await conn.execute(
                    """INSERT INTO trades
                       (timestamp, ticker, direction, side, contracts, entry_price,
                        exit_price, pnl, pnl_pct, fees, exit_reason, conviction,
                        regime_at_entry, candles_held, entry_obi, entry_roc, closed_at)
                       VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
                    (
                        trade.ticker, trade.direction,
                        "yes" if trade.direction == "long" else "no",
                        trade.contracts, trade.entry_price, trade.exit_price,
                        trade.pnl, trade.pnl_pct, trade.fees, trade.exit_reason,
                        trade.conviction, trade.regime_at_entry, trade.candles_held,
                        0.0, 0.0,
                    ),
                )
        except Exception as e:
            logger.error("coordinator.persist_trade_failed", error=str(e))

    async def _persist_signal(self, state, features, decision, action: str) -> None:
        try:
            pool = self._pool
            if pool is None:
                return
            async with pool.connection() as conn:
                await conn.execute(
                    """INSERT INTO signal_log
                       (timestamp, ticker, obi_value, obi_direction, roc_direction,
                        atr_regime, decision, conviction, skip_reason, size_mult)
                       VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        state.kalshi_ticker or state.symbol,
                        features.obi,
                        decision.obi_dir.value,
                        decision.roc_dir.value,
                        self.atr_filter.current_regime,
                        action,
                        decision.conviction.value,
                        decision.skip_reason,
                        decision.size_multiplier,
                    ),
                )
        except Exception as e:
            logger.error("coordinator.persist_signal_failed", error=str(e))


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
