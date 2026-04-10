"""
Coordinator — event loop orchestration.
Single entry point that wires all subsystems together per the quant-developer skill.
Strict order of operations: regime -> exits -> entries -> heartbeat.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
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
from execution.live_trader import LiveTrader
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

        self.paper_sizer = PositionSizer(settings.bot.initial_bankroll)
        self.live_sizer = PositionSizer(settings.bot.initial_bankroll)

        self.paper_breaker = CircuitBreaker(self.paper_sizer)
        self.live_breaker = CircuitBreaker(self.live_sizer)

        self.paper_trader = PaperTrader(self.paper_sizer)
        self.live_trader = LiveTrader(self.live_sizer)

        self.trading_mode = settings.bot.trading_mode
        self.trading_paused = False
        self.param_overrides: dict = {}
        self._pool = None
        self._tick_count = 0
        self._last_decision = None
        self._last_exit_tick = -999
        self._last_regime: Optional[str] = None
        self._cb_was_halted = False
        self._recent_exit_times: list[float] = []
        self._rapid_fire_count = 0

    @property
    def active_trader(self):
        return self.live_trader if self.trading_mode == "live" else self.paper_trader

    @property
    def position_sizer(self) -> PositionSizer:
        return self.live_sizer if self.trading_mode == "live" else self.paper_sizer

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self.live_breaker if self.trading_mode == "live" else self.paper_breaker

    async def start(self):
        self._pool = await get_pool()
        await self._restore_state()
        self.data_manager.add_listener(self._on_market_update)
        await self.data_manager.start()
        asyncio.create_task(self._schedule_tuning())
        asyncio.create_task(self._schedule_daily_attribution())
        asyncio.create_task(self._schedule_weekly_digest())
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

        # 2. On candle close: update ATR regime and persist candle
        if completed_candle:
            if self._pool is not None:
                asyncio.create_task(self._persist_candle(symbol, completed_candle))

            old_regime = self.atr_filter.current_regime
            self.atr_filter.update(
                completed_candle.high,
                completed_candle.low,
                completed_candle.close,
            )
            new_regime = self.atr_filter.current_regime
            trader = self.active_trader
            if trader.has_position:
                trader.position.candles_held += 1

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
        trader = self.active_trader
        if trader.has_position:
            exit_reason = self._check_exits(state, features, regime)
            if exit_reason:
                exit_price = self._get_exit_price(state)
                if exit_price is not None:
                    if self.trading_mode == "live":
                        trade = asyncio.ensure_future(trader.exit(exit_price, exit_reason))
                        asyncio.create_task(self._handle_live_exit(trade, symbol))
                    else:
                        trade = trader.exit(exit_price, exit_reason)
                        if trade:
                            self._on_trade_exit(trade, symbol)

        # 4. Evaluate entry signals — cooldown of 100 ticks (~2+ min) after exit
        #    Skip new entries when trading is manually paused
        #    Block entries when order book is empty or Kalshi data is stale
        if not trader.has_position and not self.trading_paused:
            ticks_since_exit = self._tick_count - self._last_exit_tick
            book_healthy = self._is_book_healthy(state)
            if ticks_since_exit > 100 and book_healthy:
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

    def _on_trade_exit(self, trade, symbol: str) -> None:
        """Common post-exit logic for both paper and live trades."""
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

    async def _handle_live_exit(self, trade_future, symbol: str) -> None:
        """Await a live trader exit (async) then run common post-exit logic."""
        try:
            trade = await trade_future
            if trade:
                self._on_trade_exit(trade, symbol)
        except Exception as e:
            logger.error("coordinator.live_exit_failed", error=str(e))

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

        overrides = self.param_overrides or None

        obi_dir = evaluate_obi(
            obi_history=obi_history,
            total_book_volume=total_vol,
            atr_regime=regime,
            has_position=False,
            overrides=overrides,
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
            overrides=overrides,
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
                trader = self.active_trader

                if self.trading_mode == "live":
                    asyncio.create_task(self._handle_live_entry(
                        trader, ticker, decision, entry_price, regime,
                        features, roc_val, symbol, state,
                    ))
                else:
                    pos = trader.enter(
                        ticker=ticker,
                        direction=decision.direction.value,
                        price=entry_price,
                        conviction=decision.conviction.value,
                        regime=regime,
                        obi=features.obi,
                        roc=roc_val,
                    )
                    if pos:
                        self._on_trade_entry(pos, symbol, state, features, decision, roc_val)
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

    def _on_trade_entry(self, pos, symbol, state, features, decision, roc_val) -> None:
        """Common post-entry logic for both paper and live trades."""
        asyncio.create_task(self._persist_signal(state, features, decision, "ENTRY"))
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

    async def _handle_live_entry(self, trader, ticker, decision, entry_price,
                                  regime, features, roc_val, symbol, state) -> None:
        """Await a live trader entry (async) then run common post-entry logic."""
        try:
            pos = await trader.enter(
                ticker=ticker,
                direction=decision.direction.value,
                price=entry_price,
                conviction=decision.conviction.value,
                regime=regime,
                obi=features.obi,
                roc=roc_val,
            )
            if pos:
                self._on_trade_entry(pos, symbol, state, features, decision, roc_val)
            else:
                await get_notifier().position_sizing_failed(
                    size_dollars=self.position_sizer.calculate_size(decision.conviction.value),
                    price=entry_price,
                    bankroll=self.position_sizer.bankroll,
                )
        except Exception as e:
            logger.error("coordinator.live_entry_failed", error=str(e))

    def _check_exits(self, state, features, regime: str) -> Optional[str]:
        pos = self.active_trader.position
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

    def _is_book_healthy(self, state) -> bool:
        """Reject entries when the order book is empty or Kalshi data is stale."""
        ob = state.order_book
        if ob.best_yes_bid is None or ob.best_yes_ask is None:
            return False

        kalshi_ws = self.data_manager._kalshi_ws
        if kalshi_ws and kalshi_ws.last_message_time is not None:
            age = time.time() - kalshi_ws.last_message_time
            if age > 60:
                logger.warning("coordinator.kalshi_stale", age_sec=round(age, 1))
                return False

        return True

    def _get_entry_price(self, state, direction) -> Optional[float]:
        """Get entry price: buy YES at ask for LONG, buy NO (sell YES at bid) for SHORT."""
        if direction == Direction.LONG:
            return state.order_book.best_yes_ask
        else:
            return state.order_book.best_yes_bid

    def _get_exit_price(self, state) -> Optional[float]:
        """Get exit price based on current position direction."""
        pos = self.active_trader.position
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

            import json as _json
            bids_json = _json.dumps([list(p) for p in state.order_book.top_n_bids(10)])
            asks_json = _json.dumps([list(p) for p in state.order_book.top_n_asks(10)])

            async with pool.connection() as conn:
                await conn.execute(
                    """INSERT INTO ob_snapshots
                       (timestamp, ticker, bids, asks, obi, total_bid_vol, total_ask_vol, spread_cents)
                       VALUES (NOW(), %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s)""",
                    (
                        state.kalshi_ticker or symbol,
                        bids_json,
                        asks_json,
                        features.obi,
                        features.total_bid_vol,
                        features.total_ask_vol,
                        features.spread_cents,
                    ),
                )
        except Exception as e:
            logger.error("coordinator.persist_failed", error=str(e))
            asyncio.create_task(get_notifier().db_error("persist_snapshot", str(e)))

    async def _persist_candle(self, symbol: str, candle) -> None:
        try:
            pool = self._pool
            if pool is None:
                return
            from datetime import datetime, timezone
            ts = datetime.fromtimestamp(candle.timestamp, tz=timezone.utc)
            async with pool.connection() as conn:
                await conn.execute(
                    """INSERT INTO candles (timestamp, source, symbol, open, high, low, close, volume)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    (ts, "live_spot", symbol, candle.open, candle.high,
                     candle.low, candle.close, candle.volume),
                )
        except Exception as e:
            logger.error("coordinator.persist_candle_failed", error=str(e))

    async def _schedule_tuning(self) -> None:
        """Periodic tuning task — runs every TUNING_INTERVAL_HOURS."""
        interval_sec = settings.bot.tuning_interval_hours * 3600
        min_candles = 2000

        while True:
            await asyncio.sleep(interval_sec)
            try:
                pool = self._pool
                if pool is None:
                    continue
                from backtesting.data_loader import load_candles_db, load_ob_snapshots_db
                candles = await load_candles_db(pool, symbol="BTC", source="live_spot,binance")
                if len(candles) < min_candles:
                    logger.info("coordinator.tuning_skipped", reason="insufficient_candles",
                                count=len(candles), required=min_candles)
                    continue

                ob_history = await load_ob_snapshots_db(pool)
                from backtesting.auto_tuner import run_tuning_cycle
                result = await run_tuning_cycle(
                    candles, ob_history, pool=pool, auto_apply=False,
                )

                notifier = get_notifier()
                msg = (
                    f"Tuning cycle complete: consistency={result.edge_consistency:.1%}, "
                    f"OOS Sharpe={result.avg_oos_sharpe:.2f}, "
                    f"should_apply={result.should_apply}, reason={result.reason}"
                )
                if result.changes:
                    changes_str = ", ".join(
                        f"{k}: {v['from']}->{v['to']}" for k, v in result.changes.items()
                    )
                    msg += f"\nChanges: {changes_str}"
                await notifier.send_heartbeat(msg)

                # Run signal health check alongside tuning
                try:
                    from monitoring.signal_health import run_signal_health_check
                    alerts = await run_signal_health_check(pool)
                    if alerts:
                        await notifier.send_heartbeat(
                            f"Signal health alerts: {'; '.join(alerts)}"
                        )
                except Exception:
                    pass

                logger.info("coordinator.tuning_complete",
                            consistency=result.edge_consistency,
                            sharpe=result.avg_oos_sharpe,
                            should_apply=result.should_apply)
            except Exception as e:
                logger.error("coordinator.tuning_failed", error=str(e))

    async def _schedule_daily_attribution(self) -> None:
        """Run attribution on yesterday's trades at midnight UTC each day."""
        while True:
            now_utc = datetime.now(timezone.utc)
            next_midnight = (now_utc + timedelta(days=1)).replace(
                hour=0, minute=5, second=0, microsecond=0
            )
            wait_sec = (next_midnight - now_utc).total_seconds()
            await asyncio.sleep(wait_sec)

            try:
                pool = self._pool
                if pool is None:
                    continue

                yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
                date_str = yesterday.isoformat()

                async with pool.connection() as conn:
                    rows = await conn.execute(
                        """SELECT timestamp, direction, pnl, pnl_pct, fees,
                                  exit_reason, conviction, regime_at_entry,
                                  candles_held, closed_at
                           FROM trades
                           WHERE DATE(timestamp) = %s AND trading_mode = %s
                           ORDER BY timestamp""",
                        (date_str, self.trading_mode),
                    )
                    result = await rows.fetchall()

                trades = []
                for r in result:
                    trades.append({
                        "timestamp": r[0].timestamp() if r[0] else 0,
                        "direction": r[1],
                        "pnl": float(r[2]) if r[2] else 0,
                        "pnl_pct": float(r[3]) if r[3] else 0,
                        "fees": float(r[4]) if r[4] else 0,
                        "exit_reason": r[5],
                        "conviction": r[6],
                        "regime_at_entry": r[7],
                        "candles_held": r[8],
                        "exit_timestamp": r[9].timestamp() if r[9] else 0,
                    })

                from backtesting.attribution import run_attribution
                attr = run_attribution(trades)

                async with pool.connection() as conn:
                    await conn.execute(
                        """INSERT INTO daily_attribution
                                (date, total_trades, total_pnl, attribution, trading_mode)
                           VALUES (%s, %s, %s, %s, %s)
                           ON CONFLICT (date) DO UPDATE
                           SET total_trades = EXCLUDED.total_trades,
                               total_pnl    = EXCLUDED.total_pnl,
                               attribution  = EXCLUDED.attribution,
                               trading_mode = EXCLUDED.trading_mode""",
                        (date_str, attr.get("total_trades", 0),
                         attr.get("total_pnl_dollars", 0),
                         json.dumps(attr), self.trading_mode),
                    )

                if trades:
                    notifier = get_notifier()
                    await notifier.daily_attribution_report(date_str, attr)

                logger.info("coordinator.daily_attribution_done",
                            date=date_str, trades=len(trades))

            except Exception as e:
                logger.error("coordinator.daily_attribution_failed", error=str(e))

    async def _schedule_weekly_digest(self) -> None:
        """Post a weekly attribution digest to Discord every Sunday at 00:10 UTC."""
        while True:
            now_utc = datetime.now(timezone.utc)
            days_until_sunday = (6 - now_utc.weekday()) % 7
            if days_until_sunday == 0 and now_utc.hour >= 1:
                days_until_sunday = 7
            next_sunday = (now_utc + timedelta(days=days_until_sunday)).replace(
                hour=0, minute=10, second=0, microsecond=0
            )
            wait_sec = (next_sunday - now_utc).total_seconds()
            await asyncio.sleep(max(wait_sec, 60))

            try:
                pool = self._pool
                if pool is None:
                    continue

                week_end = (datetime.now(timezone.utc) - timedelta(days=1)).date()
                week_start = week_end - timedelta(days=6)

                async with pool.connection() as conn:
                    rows = await conn.execute(
                        """SELECT date, total_trades, total_pnl, attribution
                           FROM daily_attribution
                           WHERE date >= %s AND date <= %s
                           ORDER BY date""",
                        (week_start.isoformat(), week_end.isoformat()),
                    )
                    result = await rows.fetchall()

                if not result:
                    logger.info("coordinator.weekly_digest_skipped", reason="no_daily_rows")
                    continue

                total_pnl = sum(float(r[2]) for r in result)
                total_trades = sum(int(r[1]) for r in result)

                conviction_pnl: dict[str, float] = {}
                regime_pnl: dict[str, float] = {}
                session_pnl: dict[str, float] = {}
                total_fees = 0.0
                theoretical_pnl = 0.0

                for r in result:
                    attr = json.loads(r[3]) if isinstance(r[3], str) else r[3]

                    sig = attr.get("signal_attribution", {})
                    for conv in ("HIGH", "NORMAL", "LOW"):
                        if conv in sig:
                            conviction_pnl[conv] = conviction_pnl.get(conv, 0) + sig[conv].get("pnl_dollars", 0)

                    reg = attr.get("regime_attribution", {})
                    for regime_name, rdata in reg.items():
                        if regime_name == "best_regime":
                            continue
                        regime_pnl[regime_name] = regime_pnl.get(regime_name, 0) + rdata.get("pnl_dollars", 0)

                    sess = attr.get("session_attribution", {})
                    for sname, sdata in sess.items():
                        session_pnl[sname] = session_pnl.get(sname, 0) + sdata.get("pnl_dollars", 0)

                    exe = attr.get("execution_attribution", {})
                    total_fees += exe.get("total_fees_dollars", 0)
                    theoretical_pnl += exe.get("theoretical_pnl", 0)

                fee_drag_pct = (total_fees / theoretical_pnl * 100) if theoretical_pnl > 0 else 0

                # Detect flips: check prior week for sessions/regimes that were profitable
                prior_start = week_start - timedelta(days=7)
                prior_end = week_start - timedelta(days=1)
                flipped_sessions: list[str] = []
                flipped_regimes: list[str] = []

                async with pool.connection() as conn:
                    rows = await conn.execute(
                        """SELECT date, attribution FROM daily_attribution
                           WHERE date >= %s AND date <= %s""",
                        (prior_start.isoformat(), prior_end.isoformat()),
                    )
                    prior_rows = await rows.fetchall()

                if prior_rows:
                    prior_session_pnl: dict[str, float] = {}
                    prior_regime_pnl: dict[str, float] = {}
                    for r in prior_rows:
                        attr = json.loads(r[1]) if isinstance(r[1], str) else r[1]
                        for sname, sdata in attr.get("session_attribution", {}).items():
                            prior_session_pnl[sname] = prior_session_pnl.get(sname, 0) + sdata.get("pnl_dollars", 0)
                        for rname, rdata in attr.get("regime_attribution", {}).items():
                            if rname == "best_regime":
                                continue
                            prior_regime_pnl[rname] = prior_regime_pnl.get(rname, 0) + rdata.get("pnl_dollars", 0)

                    for s, pnl in session_pnl.items():
                        if pnl < 0 and prior_session_pnl.get(s, 0) > 0:
                            flipped_sessions.append(s)
                    for r, pnl in regime_pnl.items():
                        if pnl < 0 and prior_regime_pnl.get(r, 0) > 0:
                            flipped_regimes.append(r)

                notifier = get_notifier()
                await notifier.weekly_digest(
                    week_start=week_start.isoformat(),
                    week_end=week_end.isoformat(),
                    total_pnl=total_pnl,
                    total_trades=total_trades,
                    conviction_breakdown=conviction_pnl,
                    regime_breakdown=regime_pnl,
                    session_breakdown=session_pnl,
                    fee_drag_pct=fee_drag_pct,
                    flipped_sessions=flipped_sessions,
                    flipped_regimes=flipped_regimes,
                )

                logger.info("coordinator.weekly_digest_sent",
                            period=f"{week_start} to {week_end}",
                            trades=total_trades, pnl=total_pnl)

            except Exception as e:
                logger.error("coordinator.weekly_digest_failed", error=str(e))

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

            mode = self.trading_mode

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
                            closed_at, error_reason, flagged_at, trading_mode)
                           VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, NOW(), %s)""",
                        (
                            trade.ticker, trade.direction,
                            "yes" if trade.direction == "long" else "no",
                            trade.contracts, trade.entry_price, trade.exit_price,
                            trade.pnl, trade.pnl_pct, trade.fees, trade.exit_reason,
                            trade.conviction, trade.regime_at_entry, trade.candles_held,
                            0.0, 0.0, error_reason, mode,
                        ),
                    )
                logger.warning(
                    "coordinator.trade_quarantined",
                    ticker=trade.ticker,
                    reason=error_reason,
                    pnl=trade.pnl,
                    rapid_count=self._rapid_fire_count,
                    mode=mode,
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
                        regime_at_entry, candles_held, entry_obi, entry_roc, closed_at, trading_mode)
                       VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)""",
                    (
                        trade.ticker, trade.direction,
                        "yes" if trade.direction == "long" else "no",
                        trade.contracts, trade.entry_price, trade.exit_price,
                        trade.pnl, trade.pnl_pct, trade.fees, trade.exit_reason,
                        trade.conviction, trade.regime_at_entry, trade.candles_held,
                        0.0, 0.0, mode,
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
                       (timestamp, bankroll, peak_bankroll, drawdown_pct, daily_pnl, trade_count, trading_mode)
                       VALUES (NOW(), %s, %s, %s, %s, %s, %s)""",
                    (
                        sizer.bankroll,
                        sizer.peak_bankroll,
                        round(sizer.current_drawdown * 100, 4),
                        round(sum(sizer.trades_today), 4),
                        len(self.active_trader.trades),
                        self.trading_mode,
                    ),
                )
        except Exception as e:
            logger.error("coordinator.persist_equity_failed", error=str(e))

    async def _save_state(self) -> None:
        """Persist bankroll state for both paper and live sizers."""
        try:
            pool = self._pool
            if pool is None:
                return
            import json

            def _sizer_dict(sizer: PositionSizer) -> dict:
                return {
                    "bankroll": sizer.bankroll,
                    "peak_bankroll": sizer.peak_bankroll,
                    "daily_start_bankroll": sizer.daily_start_bankroll,
                    "weekly_start_bankroll": sizer.weekly_start_bankroll,
                }

            state = {
                "paper": _sizer_dict(self.paper_sizer),
                "live": _sizer_dict(self.live_sizer),
                "trading_mode": self.trading_mode,
                "trading_paused": self.trading_paused,
            }
            async with pool.connection() as conn:
                await conn.execute(
                    """INSERT INTO bot_state (key, value, updated_at)
                       VALUES ('sizer_state', %s::jsonb, NOW())
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                    (json.dumps(state),),
                )
            logger.info("coordinator.state_saved",
                        paper_bankroll=state["paper"]["bankroll"],
                        live_bankroll=state["live"]["bankroll"],
                        mode=self.trading_mode)
        except Exception as e:
            logger.error("coordinator.save_state_failed", error=str(e))

    def _apply_sizer_state(self, sizer: PositionSizer, data: dict) -> None:
        initial = settings.bot.initial_bankroll
        sizer.bankroll = data.get("bankroll", initial)
        sizer.peak_bankroll = data.get("peak_bankroll", sizer.bankroll)
        sizer.daily_start_bankroll = data.get("daily_start_bankroll", sizer.bankroll)
        sizer.weekly_start_bankroll = data.get("weekly_start_bankroll", sizer.bankroll)

    async def _restore_state(self) -> None:
        """Restore bankroll from bot_state for both paper and live sizers."""
        try:
            pool = self._pool
            if pool is None:
                return
            import json

            # Restore param_overrides from auto-tuner
            try:
                async with pool.connection() as conn:
                    po_row = await conn.execute(
                        "SELECT value FROM bot_state WHERE key = 'param_overrides'"
                    )
                    po_result = await po_row.fetchone()
                if po_result:
                    val = po_result[0]
                    self.param_overrides = val if isinstance(val, dict) else json.loads(val)
                    logger.info("coordinator.param_overrides_loaded", overrides=self.param_overrides)
            except Exception as e:
                logger.warning("coordinator.param_overrides_load_failed", error=str(e))

            async with pool.connection() as conn:
                row = await conn.execute(
                    "SELECT value FROM bot_state WHERE key = 'sizer_state'"
                )
                result = await row.fetchone()

            if result:
                state = result[0] if isinstance(result[0], dict) else json.loads(result[0])

                if "paper" in state:
                    self._apply_sizer_state(self.paper_sizer, state["paper"])
                    self._apply_sizer_state(self.live_sizer, state["live"])
                    self.trading_paused = state.get("trading_paused", False)
                    logger.info("coordinator.state_restored",
                                paper_bankroll=self.paper_sizer.bankroll,
                                live_bankroll=self.live_sizer.bankroll)
                else:
                    # Legacy format: single sizer state, apply to paper only
                    self._apply_sizer_state(self.paper_sizer, state)
                    logger.info("coordinator.state_restored_legacy",
                                bankroll=self.paper_sizer.bankroll)
            else:
                async with pool.connection() as conn:
                    row = await conn.execute(
                        "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE trading_mode = 'paper'"
                    )
                    paper_pnl = float((await row.fetchone())[0])
                    row = await conn.execute(
                        "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE trading_mode = 'live'"
                    )
                    live_pnl = float((await row.fetchone())[0])

                initial = settings.bot.initial_bankroll
                if paper_pnl != 0:
                    self.paper_sizer.bankroll = initial + paper_pnl
                    self.paper_sizer.peak_bankroll = max(self.paper_sizer.bankroll, initial)
                if live_pnl != 0:
                    self.live_sizer.bankroll = initial + live_pnl
                    self.live_sizer.peak_bankroll = max(self.live_sizer.bankroll, initial)
                logger.info("coordinator.state_reconstructed",
                            paper_bankroll=self.paper_sizer.bankroll,
                            live_bankroll=self.live_sizer.bankroll)
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
