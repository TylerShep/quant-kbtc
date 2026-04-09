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
from notifications import get_notifier

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
        self._last_regime: Optional[str] = None
        self._cb_was_halted = False
        self._recent_exit_times: list[float] = []
        self._rapid_fire_count = 0

    async def start(self):
        self._pool = await get_pool()
        await self._restore_state()
        self.data_manager.add_listener(self._on_market_update)
        await self.data_manager.start()
        logger.info("coordinator.started")

    async def stop(self):
        await self._save_state()
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
            old_regime = self.atr_filter.current_regime
            self.atr_filter.update(
                completed_candle.high,
                completed_candle.low,
                completed_candle.close,
            )
            new_regime = self.atr_filter.current_regime
            if self.paper_trader.has_position:
                self.paper_trader.position.candles_held += 1

            if self._last_regime is not None and new_regime != old_regime:
                atr_val = (
                    sum(self.atr_filter.atr_pct_history) / len(self.atr_filter.atr_pct_history)
                    if self.atr_filter.atr_pct_history else None
                )
                asyncio.create_task(get_notifier().atr_regime_changed(
                    old_regime=old_regime,
                    new_regime=new_regime,
                    atr_value=atr_val,
                ))
            self._last_regime = new_regime

            logger.info(
                "candle.closed",
                o=round(completed_candle.open, 2),
                h=round(completed_candle.high, 2),
                l=round(completed_candle.low, 2),
                c=round(completed_candle.close, 2),
                regime=new_regime,
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
                        asyncio.create_task(self._persist_equity())
                        asyncio.create_task(self._save_state())
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
                        asyncio.create_task(get_notifier().trade_closed(
                            ticker=trade.ticker,
                            direction=trade.direction,
                            contracts=trade.contracts,
                            entry_price=trade.entry_price,
                            exit_price=trade.exit_price,
                            pnl=trade.pnl,
                            pnl_pct=trade.pnl_pct,
                            exit_reason=trade.exit_reason,
                            candles_held=trade.candles_held,
                            bankroll=self.position_sizer.bankroll,
                        ))

        # 4. Evaluate entry signals — cooldown of 100 ticks (~2+ min) after exit
        if not self.paper_trader.has_position:
            ticks_since_exit = self._tick_count - self._last_exit_tick
            if ticks_since_exit > 100:
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

        # 7. Persist equity snapshot every ~60 ticks and save state every ~300 ticks
        if self._tick_count % 60 == 0 and self._pool is not None:
            asyncio.create_task(self._persist_equity())
        if self._tick_count % 300 == 0 and self._pool is not None:
            asyncio.create_task(self._save_state())

    def _evaluate_entry(self, symbol: str, state, features, regime: str) -> None:
        can_trade, halt_reason = self.circuit_breaker.can_trade()

        if not can_trade and not self._cb_was_halted:
            self._cb_was_halted = True
            sizer = self.position_sizer
            asyncio.create_task(get_notifier().circuit_breaker_tripped(
                reason=halt_reason or "UNKNOWN",
                daily_loss_pct=sizer.daily_loss,
                weekly_loss_pct=sizer.weekly_loss,
                drawdown_pct=sizer.current_drawdown,
                bankroll=sizer.bankroll,
            ))
        elif can_trade and self._cb_was_halted:
            self._cb_was_halted = False
            asyncio.create_task(get_notifier().circuit_breaker_cleared(
                bankroll=self.position_sizer.bankroll,
            ))

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
                    asyncio.create_task(get_notifier().trade_opened(
                        ticker=pos.ticker,
                        direction=pos.direction,
                        contracts=pos.contracts,
                        entry_price=pos.entry_price,
                        conviction=pos.conviction,
                        obi=features.obi,
                        roc=roc_val,
                    ))
                else:
                    asyncio.create_task(get_notifier().position_sizing_failed(
                        size_dollars=self.position_sizer.calculate_size(decision.conviction.value),
                        price=entry_price,
                        bankroll=self.position_sizer.bankroll,
                    ))
        elif decision.skip_reason and self._tick_count % 60 == 0:
            asyncio.create_task(self._persist_signal(
                state, features, decision, decision.skip_reason
            ))

    def _check_exits(self, state, features, regime: str) -> Optional[str]:
        pos = self.paper_trader.position
        if pos is None:
            return None

        # Must hold for at least 1 candle before any exit (except volatility spike)
        if pos.candles_held < 1 and regime != "HIGH":
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
            asyncio.create_task(get_notifier().db_error("persist_snapshot", str(e)))

    def _detect_rapid_fire(self) -> bool:
        """Returns True if we're in a rapid-fire loop (3+ exits in 60s)."""
        now = time.time()
        self._recent_exit_times = [t for t in self._recent_exit_times if now - t < 60]
        self._recent_exit_times.append(now)
        if len(self._recent_exit_times) >= 3:
            self._rapid_fire_count += 1
            return True
        self._rapid_fire_count = 0
        return False

    async def _persist_trade(self, trade) -> None:
        try:
            pool = self._pool
            if pool is None:
                return

            is_rapid = self._detect_rapid_fire()
            error_reason = None
            if is_rapid:
                error_reason = "RAPID_FIRE_LOOP"
            elif trade.candles_held == 0 and trade.exit_reason == "STOP_LOSS":
                error_reason = "INSTANT_STOP_LOSS"

            if error_reason:
                self.position_sizer.reverse_trade(trade.pnl)
                async with pool.connection() as conn:
                    await conn.execute(
                        """INSERT INTO errored_trades
                           (timestamp, ticker, direction, side, contracts, entry_price,
                            exit_price, pnl, pnl_pct, fees, exit_reason, conviction,
                            regime_at_entry, candles_held, entry_obi, entry_roc,
                            closed_at, error_reason, flagged_at)
                           VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, NOW())""",
                        (
                            trade.ticker, trade.direction,
                            "yes" if trade.direction == "long" else "no",
                            trade.contracts, trade.entry_price, trade.exit_price,
                            trade.pnl, trade.pnl_pct, trade.fees, trade.exit_reason,
                            trade.conviction, trade.regime_at_entry, trade.candles_held,
                            0.0, 0.0, error_reason,
                        ),
                    )
                logger.warning(
                    "coordinator.trade_quarantined",
                    ticker=trade.ticker,
                    reason=error_reason,
                    pnl=trade.pnl,
                    rapid_count=self._rapid_fire_count,
                )
                asyncio.create_task(get_notifier().trade_quarantined(
                    ticker=trade.ticker,
                    direction=trade.direction,
                    pnl=trade.pnl,
                    error_reason=error_reason,
                    rapid_count=self._rapid_fire_count,
                ))
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
            asyncio.create_task(get_notifier().db_error("persist_trade", str(e)))

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
            asyncio.create_task(get_notifier().db_error("persist_signal", str(e)))

    async def _persist_equity(self) -> None:
        try:
            pool = self._pool
            if pool is None:
                return
            sizer = self.position_sizer
            async with pool.connection() as conn:
                await conn.execute(
                    """INSERT INTO bankroll_history
                       (timestamp, bankroll, peak_bankroll, drawdown_pct, daily_pnl, trade_count)
                       VALUES (NOW(), %s, %s, %s, %s, %s)""",
                    (
                        sizer.bankroll,
                        sizer.peak_bankroll,
                        round(sizer.current_drawdown * 100, 4),
                        round(sum(sizer.trades_today), 4),
                        len(self.paper_trader.trades),
                    ),
                )
        except Exception as e:
            logger.error("coordinator.persist_equity_failed", error=str(e))

    async def _save_state(self) -> None:
        """Persist bankroll state to bot_state table for recovery after restart."""
        try:
            pool = self._pool
            if pool is None:
                return
            import json
            state = {
                "bankroll": self.position_sizer.bankroll,
                "peak_bankroll": self.position_sizer.peak_bankroll,
                "daily_start_bankroll": self.position_sizer.daily_start_bankroll,
                "weekly_start_bankroll": self.position_sizer.weekly_start_bankroll,
            }
            async with pool.connection() as conn:
                await conn.execute(
                    """INSERT INTO bot_state (key, value, updated_at)
                       VALUES ('sizer_state', %s::jsonb, NOW())
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                    (json.dumps(state),),
                )
            logger.info("coordinator.state_saved", bankroll=state["bankroll"])
        except Exception as e:
            logger.error("coordinator.save_state_failed", error=str(e))

    async def _restore_state(self) -> None:
        """Restore bankroll from bot_state, or reconstruct from trade history."""
        try:
            pool = self._pool
            if pool is None:
                return
            import json
            async with pool.connection() as conn:
                row = await conn.execute(
                    "SELECT value FROM bot_state WHERE key = 'sizer_state'"
                )
                result = await row.fetchone()

            if result:
                state = result[0] if isinstance(result[0], dict) else json.loads(result[0])
                self.position_sizer.bankroll = state.get("bankroll", settings.bot.initial_bankroll)
                self.position_sizer.peak_bankroll = state.get("peak_bankroll", self.position_sizer.bankroll)
                self.position_sizer.daily_start_bankroll = state.get("daily_start_bankroll", self.position_sizer.bankroll)
                self.position_sizer.weekly_start_bankroll = state.get("weekly_start_bankroll", self.position_sizer.bankroll)
                logger.info("coordinator.state_restored", bankroll=self.position_sizer.bankroll)
            else:
                async with pool.connection() as conn:
                    row = await conn.execute("SELECT COALESCE(SUM(pnl), 0) FROM trades")
                    total_pnl = float((await row.fetchone())[0])
                if total_pnl != 0:
                    self.position_sizer.bankroll = settings.bot.initial_bankroll + total_pnl
                    self.position_sizer.peak_bankroll = max(self.position_sizer.bankroll, settings.bot.initial_bankroll)
                    logger.info("coordinator.state_reconstructed", bankroll=self.position_sizer.bankroll, total_pnl=total_pnl)
        except Exception as e:
            logger.warning("coordinator.restore_state_failed", error=str(e))


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
