"""
Coordinator — event loop orchestration.
Single entry point that wires all subsystems together per the quant-developer skill.
Strict order of operations: regime -> exits -> entries -> heartbeat.

Paper trading runs continuously regardless of mode. Live trading only runs
when trading_mode == "live". Both lanes share the same signal generation
(OBI, ROC, ATR regime) but maintain independent positions, sizers, and breakers.
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
from strategies.resolver import SignalConflictResolver, Conviction
from strategies.spread_div import evaluate_spread_divergence, SpreadState
from filters.atr_regime import ATRRegimeFilter
from filters.spread_regime import SpreadRegimeFilter
from risk.position_sizer import PositionSizer
from risk.circuit_breaker import CircuitBreaker
from execution.paper_trader import PaperTrader
from execution.live_trader import LiveTrader
from api.ws import ws_manager
from database import get_pool, close_pool
from notifications import get_notifier
from filters.price_guard import PriceGuard
from filters.trend_guard import TrendGuard
from ml.feature_capture import extract_features, save_features, label_trade
from data.historical_sync import HistoricalSync

logger = structlog.get_logger(__name__)


class Coordinator:
    """Wires data feeds -> features -> strategy -> risk -> execution -> dashboard."""

    def __init__(self):
        self.data_manager = DataManager()
        self.candle_aggregator = CandleAggregator()
        self.feature_engine = FeatureEngine()
        self.atr_filter = ATRRegimeFilter()
        self.spread_filter = SpreadRegimeFilter()
        self.price_guard = PriceGuard()
        self.trend_guard = TrendGuard()
        self.resolver = SignalConflictResolver()

        self.paper_sizer = PositionSizer(settings.bot.initial_bankroll)
        self.live_sizer = PositionSizer(settings.bot.initial_bankroll)

        self.paper_breaker = CircuitBreaker(self.paper_sizer, never_halt=True)
        self.live_breaker = CircuitBreaker(self.live_sizer)

        self.paper_trader = PaperTrader(self.paper_sizer)
        self.live_trader = LiveTrader(self.live_sizer)

        self.trading_mode = settings.bot.trading_mode
        self.trading_paused = "off"  # "off" | "settling" | "paused"
        self.param_overrides: dict = {}
        self._pool = None
        self._tick_count = 0
        self._last_paper_decision = None
        self._last_live_decision = None
        self._last_paper_exit_tick = -999
        self._last_live_exit_tick = -999
        self._last_regime: Optional[str] = None
        self._cb_was_halted = False
        self._recent_exit_times: list[float] = []
        self._rapid_fire_count = 0
        self._orphan_check_in_flight = False

        # Fix 1: Duplicate entry guard — prevents concurrent live entry tasks
        self._live_entry_in_flight = False
        self._live_exit_in_flight = False

        # ML feature snapshots: keyed by ticker, captured at entry, consumed at exit
        self._pending_features: dict[str, dict] = {}

        # One-time alert: fires when 500+ fully-labeled paper trades exist
        self._ml_data_ready_sent: bool = False

        self.historical_sync = HistoricalSync()


    @property
    def active_trader(self):
        return self.live_trader if self.trading_mode == "live" else self.paper_trader

    @property
    def position_sizer(self) -> PositionSizer:
        return self.live_sizer if self.trading_mode == "live" else self.paper_sizer

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self.live_breaker if self.trading_mode == "live" else self.paper_breaker

    @property
    def live_enabled(self) -> bool:
        return self.trading_mode == "live"

    async def sync_live_bankroll(self, is_initial: bool = False) -> float:
        """Fetch real Kalshi wallet balance and update live_sizer.

        The Kalshi wallet is the source of truth. Peak bankroll tracks the
        high-water mark from real trading, but is capped to the wallet on
        sync so that external losses (orphaned trades, manual withdrawals)
        don't cause a permanent drawdown halt.

        daily/weekly baselines are only set on initial sync (startup) or
        when explicitly requested — not on every sync call.
        """
        balance_data = await self.live_trader.client.get_balance()
        wallet = float(balance_data.get("balance", 0)) / 100
        if wallet > 0:
            self.live_sizer.bankroll = wallet
            old_peak = self.live_sizer.peak_bankroll
            if old_peak == settings.bot.initial_bankroll:
                self.live_sizer.peak_bankroll = wallet
            else:
                self.live_sizer.peak_bankroll = max(wallet, old_peak)
            if is_initial:
                self.live_sizer.daily_start_bankroll = wallet
                self.live_sizer.weekly_start_bankroll = wallet
            logger.info("coordinator.live_bankroll_synced",
                        wallet=wallet, peak=self.live_sizer.peak_bankroll)
        return wallet

    async def start(self):
        self._pool = await get_pool()
        self.live_trader.position_manager.set_db_pool(self._pool)
        await self.live_trader.position_manager.restore_state()
        await self._restore_state()
        await self._warmup_atr()
        await self._warmup_spread_filter()
        self.data_manager.add_listener(self._on_market_update)
        await self.data_manager.start()
        asyncio.create_task(self._schedule_tuning())
        asyncio.create_task(self._schedule_daily_attribution())
        asyncio.create_task(self._schedule_weekly_digest())
        asyncio.create_task(self._schedule_paper_sizer_resets())
        await self.historical_sync.start(self._pool)
        logger.info("coordinator.started")

    async def stop(self):
        await self._save_state()
        await self.data_manager.stop()
        try:
            await self.live_trader.client.aclose()
        except Exception:
            pass
        await close_pool()
        logger.info("coordinator.stopped")

    # ── Main tick pipeline ─────────────────────────────────────────────────

    def _on_market_update(self, symbol: str, state) -> None:
        """Called on every market data update.

        Paper lane always runs. Live lane only runs when trading_mode == "live".
        Both lanes share the same features/signals but maintain independent state.
        """
        self._tick_count += 1

        # ── 0. Settlement / expiry guards (both lanes) ─────────────────
        self._run_settlement_guards(symbol, state, self.paper_trader, "paper")
        if self.live_enabled:
            self._run_settlement_guards(symbol, state, self.live_trader, "live")

        # Settling check: only applies to live lane
        if self.trading_paused == "settling" and not self.live_trader.has_position:
            self.trading_paused = "paused"
            logger.info("coordinator.settling_complete", source="tick_safety")
            asyncio.create_task(ws_manager.broadcast({
                "type": "settling_complete",
                "trading_paused": "paused",
            }))
            asyncio.create_task(self._save_state())

        # ── Feature gating ─────────────────────────────────────────────
        features = self.feature_engine.update(symbol, state)
        if features is None:
            return

        self.spread_filter.update(features.spread_cents)

        # ── 1. Candle aggregator ───────────────────────────────────────
        completed_candle = None
        if state.spot_price:
            completed_candle = self.candle_aggregator.on_tick(
                time.time(), state.spot_price
            )

        # ── 2. ATR regime on candle close ──────────────────────────────
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

            if self.paper_trader.has_position:
                self.paper_trader.position.candles_held += 1
            if self.live_trader.has_position:
                self.live_trader.position.candles_held += 1

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

        regime = self.atr_filter.current_regime

        # ── 3. Paper lane: exits + entries (always runs) ───────────────
        self._run_paper_lane(symbol, state, features, regime)

        # ── 4. Live lane: exits + entries (only when live) ─────────────
        if self.live_enabled:
            self._run_live_lane(symbol, state, features, regime)

        # ── 5. Broadcast to dashboard ──────────────────────────────────
        asyncio.create_task(
            ws_manager.broadcast({
                "type": "market_update",
                "symbol": symbol,
                "data": features.to_dict(),
                "state": _serialize_state(state),
                "decision": self._serialize_decision("paper"),
                "live_decision": self._serialize_decision("live") if self.live_enabled else None,
            })
        )

        # ── 6. Periodic tasks ─────────────────────────────────────────
        if self._tick_count % 10 == 0 and self._pool is not None:
            asyncio.create_task(self._persist_snapshot(symbol, state, features))

        high_risk_window = (
            regime == "HIGH"
            or (state.time_remaining_sec is not None and state.time_remaining_sec < 300)
        )
        reconcile_interval = 15 if high_risk_window else 50

        if self._tick_count % reconcile_interval == 0 and self.live_trader.orphaned_positions and not self._orphan_check_in_flight:
            self._orphan_check_in_flight = True
            asyncio.create_task(self._check_orphaned_positions())

        if self._tick_count % 60 == 0 and self._pool is not None:
            asyncio.create_task(self._persist_equity("paper"))
            if self.live_enabled:
                asyncio.create_task(self._persist_equity("live"))
        if self._tick_count % 300 == 0 and self._pool is not None:
            asyncio.create_task(self._save_state())

        if self._tick_count % reconcile_interval == 0 and self.live_enabled and not self.live_trader.position_manager.is_busy:
            asyncio.create_task(self._periodic_reconciliation())

    # ── Settlement guards ──────────────────────────────────────────────

    def _run_settlement_guards(self, symbol: str, state, trader, mode: str) -> None:
        """Handle settlement and expiry guard for a given trader lane."""
        is_live = mode == "live"
        pm_busy = is_live and self.live_trader.position_manager.is_busy

        if state.resolved and trader.has_position and not pm_busy:
            pos = trader.position
            settled_ticker = state.kalshi_ticker
            if pos and (pos.ticker == settled_ticker or pos.ticker in (settled_ticker or "")):
                result_str = "yes" if state.resolved_outcome else "no"
                logger.info("coordinator.contract_settled",
                            ticker=pos.ticker, result=result_str,
                            settled_ticker=settled_ticker, mode=mode)
                if is_live:
                    asyncio.create_task(self._handle_settlement(
                        trader, result_str, symbol, mode))
                else:
                    trade = trader.handle_settlement(result_str)
                    if trade:
                        self._on_trade_exit(trade, symbol, mode)
                state.resolved = False
                state.resolved_outcome = None

        if trader.has_position and not pm_busy:
            pos = trader.position
            guard_sec = settings.risk.short_settlement_guard_sec
            if (pos and pos.direction == "short"
                    and state.time_remaining_sec is not None
                    and state.time_remaining_sec < guard_sec
                    and state.time_remaining_sec >= 60):
                current_price = self._get_exit_price_for(state, trader)
                if current_price is not None and current_price > pos.entry_price:
                    logger.info("coordinator.short_settlement_guard",
                                ticker=pos.ticker, entry=pos.entry_price,
                                current=current_price, remaining_sec=state.time_remaining_sec,
                                mode=mode)
                    if is_live:
                        if not self._live_exit_in_flight:
                            self._live_exit_in_flight = True
                            asyncio.create_task(self._handle_live_exit(
                                asyncio.ensure_future(trader.exit(current_price, "SHORT_SETTLEMENT_GUARD")),
                                symbol,
                                original_reason="SHORT_SETTLEMENT_GUARD",
                                exit_price=current_price,
                            ))
                    else:
                        trade = trader.exit(current_price, "SHORT_SETTLEMENT_GUARD")
                        if trade:
                            self._on_trade_exit(trade, symbol, mode)

            if pos and state.time_remaining_sec is not None and state.time_remaining_sec < 60:
                exit_price = self._get_exit_price_for(state, trader) or pos.entry_price
                if is_live:
                    if not pm_busy and not self._live_exit_in_flight:
                        self._live_exit_in_flight = True
                        asyncio.create_task(self._handle_live_exit(
                            asyncio.ensure_future(trader.exit(exit_price, "EXPIRY_GUARD")),
                            symbol,
                            original_reason="EXPIRY_GUARD",
                            exit_price=exit_price,
                        ))
                else:
                    trade = trader.exit(exit_price, "EXPIRY_GUARD")
                    if trade:
                        self._on_trade_exit(trade, symbol, mode)

    # ── Paper lane ─────────────────────────────────────────────────────

    def _run_paper_lane(self, symbol: str, state, features, regime: str) -> None:
        trader = self.paper_trader
        sizer = self.paper_sizer
        breaker = self.paper_breaker

        if trader.has_position:
            exit_reason = self._check_exits_for(state, features, regime, trader)
            if exit_reason:
                exit_price = self._get_exit_price_for(state, trader)
                if exit_price is not None:
                    trade = trader.exit(exit_price, exit_reason)
                    if trade:
                        self._on_trade_exit(trade, symbol, "paper")

        near_expiry = state.time_remaining_sec is not None and state.time_remaining_sec < 120
        if not trader.has_position and not near_expiry:
            ticks_since_exit = self._tick_count - self._last_paper_exit_tick
            book_healthy = self._is_book_healthy(state)
            if ticks_since_exit > 100 and book_healthy:
                self._evaluate_entry_for(
                    symbol, state, features, regime,
                    trader, sizer, breaker, "paper",
                )

    # ── Live lane ──────────────────────────────────────────────────────

    def _run_live_lane(self, symbol: str, state, features, regime: str) -> None:
        trader = self.live_trader
        sizer = self.live_sizer
        breaker = self.live_breaker
        pm = trader.position_manager

        if trader.has_position and not pm.is_busy and not self._live_exit_in_flight:
            exit_reason = self._check_exits_for(state, features, regime, trader)
            if exit_reason:
                exit_price = self._get_exit_price_for(state, trader)
                if exit_price is not None:
                    self._live_exit_in_flight = True
                    asyncio.create_task(self._handle_live_exit(
                        asyncio.ensure_future(trader.exit(exit_price, exit_reason)),
                        symbol,
                        original_reason=exit_reason,
                        exit_price=exit_price,
                    ))

        near_expiry = state.time_remaining_sec is not None and state.time_remaining_sec < 120
        if (pm.can_enter
                and self.trading_paused == "off"
                and not near_expiry
                and not self._live_entry_in_flight):
            ticks_since_exit = self._tick_count - self._last_live_exit_tick
            book_healthy = self._is_book_healthy(state)
            if ticks_since_exit > 100 and book_healthy:
                self._evaluate_entry_for(
                    symbol, state, features, regime,
                    trader, sizer, breaker, "live",
                )

    # ── Trade exit / entry callbacks ───────────────────────────────────

    def _on_trade_exit(self, trade, symbol: str, mode: str = "paper") -> None:
        """Common post-exit logic for both paper and live trades."""
        if mode == "live":
            self._unregister_position_ticker(trade.ticker)
            self._last_live_exit_tick = self._tick_count

            # Supervised single-trade mode: auto-pause after every live trade
            # so the operator can review before the next one is allowed.
            self.trading_paused = "paused"
            logger.info("coordinator.supervised_auto_pause",
                        ticker=trade.ticker, exit_reason=trade.exit_reason,
                        pnl=trade.pnl)
            asyncio.create_task(ws_manager.broadcast({
                "type": "supervised_pause",
                "trading_paused": "paused",
                "reason": "Post-trade review required",
                "trade_ticker": trade.ticker,
            }))
        else:
            self._last_paper_exit_tick = self._tick_count

        asyncio.create_task(self._persist_and_notify_exit(trade, symbol, mode))

    async def _persist_and_notify_exit(self, trade, symbol: str, mode: str) -> None:
        """Persist trade first, then notify. Skip Discord if trade was quarantined."""
        quarantined, trade_id = await self._persist_trade(trade, mode)

        if trade_id is not None:
            await self._save_and_label_features(trade, trade_id, mode)

        if mode == "paper" and not self._ml_data_ready_sent:
            asyncio.create_task(self._check_ml_data_threshold())

        if mode == "live":
            try:
                await self.sync_live_bankroll()
            except Exception as e:
                logger.warning("coordinator.post_exit_wallet_sync_failed", error=str(e))

        await self._persist_equity(mode)
        asyncio.create_task(self._save_state())

        if quarantined:
            if mode == "live":
                asyncio.create_task(self._send_post_trade_report(
                    trade, symbol, quarantined=True))
            return

        sizer = self.live_sizer if mode == "live" else self.paper_sizer
        asyncio.create_task(ws_manager.broadcast({
            "type": "trade_exit",
            "symbol": symbol,
            "mode": mode,
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
            bankroll=sizer.bankroll,
            mode=mode,
        ))

        if mode == "live":
            asyncio.create_task(self._send_post_trade_report(
                trade, symbol, quarantined=False))

    async def _save_and_label_features(self, trade, trade_id: int, mode: str) -> None:
        """Save pending features snapshot and label with trade outcome."""
        try:
            ticker = trade.ticker
            feat = self._pending_features.pop(ticker, None)
            if feat is None:
                return
            pool = self._pool
            if pool is None:
                return
            await save_features(
                pool,
                trade_id=trade_id,
                trading_mode=mode,
                ticker=ticker,
                feature_dict=feat,
            )
            mfe = getattr(trade, "max_favorable_excursion", 0.0)
            mae = getattr(trade, "max_adverse_excursion", 0.0)
            await label_trade(pool, trade_id, trade.pnl, mfe=mfe, mae=mae)
        except Exception as e:
            logger.warning("coordinator.ml_feature_save_failed", error=str(e))

    async def _check_ml_data_threshold(self) -> None:
        """One-time check: fire a Discord alert when 500+ fully-labeled paper trades exist."""
        try:
            pool = self._pool
            if pool is None:
                return
            async with pool.connection() as conn:
                row = await conn.execute(
                    """SELECT COUNT(*) FROM trade_features
                       WHERE trading_mode = 'paper'
                         AND label IS NOT NULL
                         AND max_favorable_excursion IS NOT NULL
                         AND max_adverse_excursion IS NOT NULL"""
                )
                count = (await row.fetchone())[0]

            if count >= 500:
                self._ml_data_ready_sent = True
                await self._save_state()

                async with pool.connection() as conn:
                    row = await conn.execute(
                        """SELECT AVG(CASE WHEN label = 1 THEN 1.0 ELSE 0.0 END)
                           FROM trade_features
                           WHERE trading_mode = 'paper' AND label IS NOT NULL"""
                    )
                    win_rate = float((await row.fetchone())[0] or 0.5)

                await get_notifier().ml_data_ready(count, win_rate)
                logger.info("coordinator.ml_data_ready_sent", rows=count, win_rate=win_rate)
        except Exception as e:
            logger.warning("coordinator.ml_data_threshold_check_failed", error=str(e))

    async def _send_post_trade_report(self, trade, symbol: str,
                                       quarantined: bool = False) -> None:
        """Generate and send a structured post-trade review to Discord.

        This fires after every live trade exit in supervised single-trade mode.
        It surfaces anomalies, exchange state, and a clear call-to-action.
        """
        pm = self.live_trader.position_manager
        anomalies = self._check_trade_anomalies(trade, pm)
        health = "CLEAN" if not anomalies else "ANOMALIES DETECTED"

        duration_str = "N/A"
        try:
            from datetime import datetime
            if hasattr(trade, "entry_time") and hasattr(trade, "exit_time"):
                et = trade.entry_time
                xt = trade.exit_time
                if isinstance(et, str):
                    et = datetime.fromisoformat(et)
                if isinstance(xt, str):
                    xt = datetime.fromisoformat(xt)
                delta = xt - et
                mins = int(delta.total_seconds() // 60)
                secs = int(delta.total_seconds() % 60)
                duration_str = f"{mins}m {secs}s"
        except Exception:
            pass

        anomaly_text = "\n".join(f"- {a}" for a in anomalies) if anomalies else "None"
        pnl_icon = "\u2705" if trade.pnl >= 0 else "\u274c"
        quarantine_badge = " [QUARANTINED]" if quarantined else ""

        notifier = get_notifier()
        embed = {
            "title": f"\U0001f50d [LIVE] Post-Trade Review{quarantine_badge} \u2014 {trade.ticker}",
            "color": 0xED4245 if anomalies else 0x57F287,
            "fields": [
                {"name": "Result", "value": f"{pnl_icon} {'+'if trade.pnl >= 0 else ''}${trade.pnl:.4f} ({trade.pnl_pct:+.2%})", "inline": True},
                {"name": "Direction", "value": trade.direction.upper(), "inline": True},
                {"name": "Contracts", "value": str(trade.contracts), "inline": True},
                {"name": "Entry / Exit", "value": f"{trade.entry_price}\u00a2 \u2192 {trade.exit_price}\u00a2", "inline": True},
                {"name": "Fees", "value": f"${trade.fees:.4f}", "inline": True},
                {"name": "Duration", "value": duration_str, "inline": True},
                {"name": "Exit Reason", "value": trade.exit_reason, "inline": True},
                {"name": "Candles Held", "value": str(trade.candles_held), "inline": True},
                {"name": "Conviction", "value": trade.conviction, "inline": True},
                {"name": "Health", "value": health, "inline": False},
                {"name": "Anomalies", "value": anomaly_text[:1000], "inline": False},
                {"name": "PM State", "value": pm.state.value, "inline": True},
                {"name": "Orphans", "value": str(len(pm.orphaned_positions)), "inline": True},
                {"name": "Bankroll", "value": f"${self.live_sizer.bankroll:.2f}", "inline": True},
            ],
            "footer": {"text": "KBTC Bot \u00b7 PAUSED \u2014 Resume trading from dashboard after review"},
        }
        await notifier._post(notifier._live_trades_url or notifier._trades_url, embed)

        if anomalies:
            logger.warning("coordinator.post_trade_anomalies",
                           ticker=trade.ticker, anomalies=anomalies)
            self._append_trade_anomaly_to_bug_log(trade, anomalies)

    def _check_trade_anomalies(self, trade, pm) -> list[str]:
        """Identify anomalies in a completed live trade for the post-trade report."""
        anomalies = []

        if pm.state != pm.state.FLAT:
            anomalies.append(f"PM state is {pm.state.value}, expected FLAT")

        if pm.has_orphans:
            tickers = [o.ticker for o in pm.orphaned_positions]
            anomalies.append(f"Orphaned positions exist: {', '.join(tickers)}")

        suspicious_exits = (
            "DESYNC", "EMERGENCY_STOP", "RETRY",
            "CONTRACT_SETTLED_VERIFY_FAILED",
        )
        if trade.exit_reason in suspicious_exits:
            anomalies.append(f"Suspicious exit reason: {trade.exit_reason}")

        if hasattr(trade, "entry_order_id") and trade.entry_order_id is None:
            anomalies.append("Missing entry_order_id (order may not have been confirmed)")

        if hasattr(trade, "exit_order_id") and trade.exit_order_id is None:
            if trade.exit_reason not in ("CONTRACT_SETTLED", "CONTRACT_SETTLED_VERIFY_FAILED"):
                anomalies.append("Missing exit_order_id (exit may not have been confirmed)")

        if trade.contracts == 0:
            anomalies.append("Zero contracts in trade result")

        if trade.pnl_pct < -0.10:
            anomalies.append(f"Large loss: {trade.pnl_pct:.2%}")

        return anomalies

    def _append_trade_anomaly_to_bug_log(self, trade, anomalies: list[str]) -> None:
        """Log a live trade anomaly to the database and (optionally) to the
        local known-bugs.mdc file if it exists on the filesystem."""
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        anomaly_text = "; ".join(anomalies)

        if self._pool is not None:
            async def _persist():
                try:
                    async with self._pool.connection() as conn:
                        await conn.execute(
                            """INSERT INTO errored_trades
                               (timestamp, ticker, direction, side, contracts, entry_price,
                                exit_price, pnl, pnl_pct, fees, exit_reason, conviction,
                                regime_at_entry, candles_held, entry_obi, entry_roc,
                                signal_driver, closed_at, error_reason, flagged_at, trading_mode)
                               VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, NOW(), %s)""",
                            (
                                trade.ticker, trade.direction,
                                "yes" if trade.direction == "long" else "no",
                                trade.contracts, trade.entry_price, trade.exit_price,
                                trade.pnl, trade.pnl_pct, trade.fees, trade.exit_reason,
                                trade.conviction, getattr(trade, "regime_at_entry", "UNKNOWN"),
                                trade.candles_held,
                                getattr(trade, "entry_obi", 0.0) or 0.0,
                                getattr(trade, "entry_roc", 0.0) or 0.0,
                                getattr(trade, "signal_driver", "-") or "-",
                                f"TRADE_ANOMALY: {anomaly_text}"[:200],
                                "live",
                            ),
                        )
                    logger.info("coordinator.anomaly_persisted_to_db",
                                ticker=trade.ticker, anomalies=anomaly_text)
                except Exception as e:
                    logger.error("coordinator.anomaly_db_persist_failed", error=str(e))
            asyncio.create_task(_persist())

        import os
        bug_log = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            ".cursor", "rules", "known-bugs.mdc",
        )
        if os.path.exists(bug_log):
            try:
                with open(bug_log, "r") as f:
                    existing = f.read()
                count = existing.count("## BUG-") + existing.count("## TRADE-ANOMALY-")
                entry_id = count + 1
                anomaly_lines = "\n".join(f"  - {a}" for a in anomalies)
                entry = (
                    f"\n## TRADE-ANOMALY-{entry_id:03d}: {trade.ticker}\n"
                    f"- **Date:** {ts}\n"
                    f"- **Ticker:** {trade.ticker}\n"
                    f"- **Direction:** {trade.direction}\n"
                    f"- **PnL:** ${trade.pnl:+.4f} ({trade.pnl_pct:+.2%})\n"
                    f"- **Exit reason:** {trade.exit_reason}\n"
                    f"- **Anomalies:**\n{anomaly_lines}\n"
                    f"- **Status:** UNDER REVIEW\n"
                )
                with open(bug_log, "a") as f:
                    f.write(entry)
                logger.info("coordinator.anomaly_logged_to_file",
                            entry_id=f"TRADE-ANOMALY-{entry_id:03d}")
            except Exception as e:
                logger.warning("coordinator.anomaly_file_write_failed", error=str(e))

    def _on_trade_entry(self, pos, symbol, state, features, decision, roc_val, mode: str = "paper") -> None:
        """Common post-entry logic for both paper and live trades."""
        if mode == "live":
            self._register_position_ticker(pos.ticker, symbol)
        asyncio.create_task(self._persist_signal(state, features, decision, "ENTRY",
                                                    roc_value=roc_val))
        asyncio.create_task(ws_manager.broadcast({
            "type": "trade_entry",
            "symbol": symbol,
            "mode": mode,
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
            mode=mode,
        ))

        try:
            feat = extract_features(
                features=features,
                candle_aggregator=self.candle_aggregator,
                atr_filter=self.atr_filter,
                state=state,
                historical_sync=self.historical_sync,
            )
            self._pending_features[pos.ticker] = feat
        except Exception as e:
            logger.warning("coordinator.feature_capture_failed", error=str(e))

    async def _handle_settlement(self, trader, result: str, symbol: str, mode: str = "live") -> None:
        """Handle exchange settlement of a live position.

        PositionManager.handle_settlement verifies against exchange and
        handles VERIFY_FAILED properly (converts to orphan instead of
        trusting internal state).
        """
        try:
            trade = await trader.handle_settlement(result)
            if trade:
                self._on_trade_exit(trade, symbol, mode)
        except Exception as e:
            logger.error("coordinator.settlement_failed", error=str(e))

    async def _handle_live_exit(
        self,
        trade_future,
        symbol: str,
        original_reason: str = "UNKNOWN",
        exit_price: Optional[float] = None,
    ) -> None:
        """Await a live trader exit (async) then run common post-exit logic.

        PositionManager handles retries, orphan conversion, and locking
        internally. This wrapper just processes the result. On retries,
        the original exit reason and price are preserved.
        """
        try:
            trade = await trade_future
            if trade:
                self._on_trade_exit(trade, symbol, "live")
                return

            if not self.live_trader.has_position:
                return

            retry_price = exit_price or self.live_trader.position.entry_price

            MAX_RETRIES = 2
            for attempt in range(1, MAX_RETRIES + 1):
                if not self.live_trader.has_position:
                    return
                delay = 2 ** attempt
                logger.warning("coordinator.live_exit_retry",
                               attempt=attempt, delay=delay,
                               reason=original_reason)
                await asyncio.sleep(delay)
                try:
                    trade = await self.live_trader.exit(retry_price, original_reason)
                    if trade:
                        self._on_trade_exit(trade, symbol, "live")
                        return
                except Exception as e:
                    logger.error("coordinator.live_exit_retry_failed",
                                 attempt=attempt, error=str(e))

            if self.live_trader.has_position:
                pos = self.live_trader.position
                logger.error("coordinator.live_exit_abandoned",
                             ticker=pos.ticker, contracts=pos.contracts)
                self._unregister_position_ticker(pos.ticker)
                self.live_trader.adopt_orphan(
                    ticker=pos.ticker,
                    direction=pos.direction,
                    contracts=pos.contracts,
                    avg_entry_price=pos.entry_price,
                )
                self.live_trader.position = None
                asyncio.create_task(get_notifier().unhandled_exception(
                    location="coordinator._handle_live_exit",
                    error=f"Exit failed after retries for {pos.ticker}, converted to orphan",
                ))
        except Exception as e:
            logger.error("coordinator.live_exit_failed", error=str(e))
        finally:
            self._live_exit_in_flight = False

    async def _is_duplicate_orphan_trade(self, ticker: str, reason: str) -> bool:
        """Check if a trade for this ticker was already recorded in the last 5 minutes."""
        if self._pool is None:
            return False
        try:
            async with self._pool.connection() as conn:
                row = await conn.execute(
                    """SELECT id FROM trades
                       WHERE ticker = %s AND trading_mode = 'live'
                       AND timestamp >= NOW() - INTERVAL '5 minutes'
                       LIMIT 1""",
                    (ticker,),
                )
                result = await row.fetchone()
                if result:
                    logger.warning("coordinator.orphan_duplicate_skipped",
                                   ticker=ticker, reason=reason,
                                   existing_trade_id=result[0])
                    return True
        except Exception as e:
            logger.warning("coordinator.orphan_dedup_check_failed",
                           ticker=ticker, error=str(e))
        return False

    async def _check_orphaned_positions(self) -> None:
        """Periodically check orphaned positions for break-even exit."""
        try:
            closed = await self.live_trader.check_orphans()
            for info in closed:
                pnl = info["pnl"]
                notional = info["contracts"] * info["entry_price"] / 100
                pnl_pct = pnl / notional if notional > 0 else 0
                fees = notional * self.live_trader.FEE_RATE

                logger.info("coordinator.orphan_recovered",
                            ticker=info["ticker"], pnl=pnl,
                            reason=info["reason"])

                if await self._is_duplicate_orphan_trade(info["ticker"], info["reason"]):
                    continue

                self.live_sizer.record_trade(pnl)

                if self._pool is not None:
                    try:
                        async with self._pool.connection() as conn:
                            await conn.execute(
                                """INSERT INTO trades
                                   (timestamp, ticker, direction, side, contracts, entry_price,
                                    exit_price, pnl, pnl_pct, fees, exit_reason, conviction,
                                    regime_at_entry, candles_held, entry_obi, entry_roc,
                                    signal_driver, closed_at, trading_mode)
                                   VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)""",
                                (
                                    info["ticker"], info["direction"],
                                    "yes" if info["direction"] == "long" else "no",
                                    info["contracts"], info["entry_price"],
                                    info["exit_price"], pnl, round(pnl_pct, 4), round(fees, 4),
                                    info["reason"], "UNKNOWN", "UNKNOWN", 0,
                                    0.0, 0.0, "UNKNOWN", "live",
                                ),
                            )
                    except Exception as e:
                        logger.error("coordinator.orphan_persist_failed", error=str(e))

                asyncio.create_task(get_notifier().trade_closed(
                    ticker=info["ticker"],
                    direction=info["direction"],
                    contracts=info["contracts"],
                    entry_price=info["entry_price"],
                    exit_price=info["exit_price"],
                    pnl=pnl,
                    pnl_pct=round(pnl_pct, 4),
                    exit_reason=info["reason"],
                    candles_held=0,
                    bankroll=self.live_sizer.bankroll,
                    mode="live",
                ))

            remaining = len(self.live_trader.orphaned_positions)
            if closed:
                logger.info("coordinator.orphan_check_complete",
                            closed=len(closed), remaining=remaining)
                try:
                    await self.sync_live_bankroll()
                except Exception as e:
                    logger.warning("coordinator.orphan_bankroll_sync_failed",
                                   error=str(e))
                await self._persist_equity("live")
        except Exception as e:
            logger.error("coordinator.orphan_check_failed", error=str(e))
        finally:
            self._orphan_check_in_flight = False

    # ── Parameterized entry / exit evaluation ──────────────────────────

    def _evaluate_entry_for(self, symbol: str, state, features, regime: str,
                            trader, sizer: PositionSizer,
                            breaker: CircuitBreaker, mode: str) -> None:
        can_trade, halt_reason = breaker.can_trade()

        if mode == "live":
            if not can_trade and not self._cb_was_halted:
                self._cb_was_halted = True
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
                    bankroll=sizer.bankroll,
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

        current_atr_pct = (
            self.atr_filter.atr_pct_history[-1]
            if self.atr_filter.atr_pct_history else None
        )

        roc_dir = evaluate_roc(
            closes=closes,
            candles=candle_list,
            atr_regime=regime,
            obi_direction=obi_dir,
            has_position=False,
            overrides=overrides,
            atr_pct=current_atr_pct,
        )

        spread_state = evaluate_spread_divergence(
            spread_history=self.spread_filter.spread_history(),
            current_spread=features.spread_cents,
            atr_regime=regime,
            overrides=overrides,
        )

        decision = self.resolver.resolve(
            obi_direction=obi_dir,
            roc_direction=roc_dir,
            atr_regime=regime,
            can_trade=can_trade,
            spread_state=spread_state,
        )

        # TFI conviction gating — downgrade when trade flow disagrees with OBI
        hs_cfg = settings.historical_sync
        if (hs_cfg.tfi_conviction_enabled
                and decision.should_trade_in(mode)
                and decision.obi_dir != Direction.NEUTRAL):
            ticker = getattr(state, "kalshi_ticker", None) or symbol
            tfi = self.historical_sync.get_tfi(ticker) if self.historical_sync else None
            if tfi is not None:
                thresh = hs_cfg.tfi_disagree_threshold
                disagrees = (
                    (decision.obi_dir == Direction.LONG and tfi < 0.5 - thresh)
                    or (decision.obi_dir == Direction.SHORT and tfi > 0.5 + thresh)
                )
                if disagrees:
                    old_conv = decision.conviction
                    new_conv = Conviction.downgrade(old_conv)
                    decision = decision.with_conviction(
                        new_conv,
                        skip_reason="TFI_DISAGREE" if new_conv == Conviction.NONE else None,
                    )
                    logger.info("coordinator.tfi_downgrade",
                                ticker=ticker, tfi=round(tfi, 4),
                                obi_dir=decision.obi_dir.value,
                                old_conviction=old_conv.value,
                                new_conviction=new_conv.value,
                                mode=mode)

        if mode == "paper":
            self._last_paper_decision = decision
        else:
            self._last_live_decision = decision

        roc_val = calculate_roc(closes, settings.roc.lookback) or 0.0

        self.trend_guard.apply_short_trend_filter(decision, closes, mode)

        if decision.should_trade_in(mode):
            entry_price = self._get_entry_price(state, decision.direction)
            if entry_price is not None and entry_price > 0:
                allowed, guard_reason = self.price_guard.is_allowed(
                    entry_price, decision.direction.value,
                    regime, state.time_remaining_sec,
                )
                if not allowed:
                    if self._tick_count % 60 == 0:
                        logger.info("coordinator.price_guard_rejected",
                                    price=entry_price, direction=decision.direction.value,
                                    reason=guard_reason, mode=mode)
                    return

                ticker = state.kalshi_ticker or symbol

                if mode == "live":
                    self._live_entry_in_flight = True
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
                        signal_driver=decision.signal_driver,
                    )
                    if pos:
                        self._on_trade_entry(pos, symbol, state, features, decision, roc_val, mode)
                    else:
                        asyncio.create_task(get_notifier().position_sizing_failed(
                            size_dollars=sizer.calculate_size(decision.conviction.value, decision.direction.value),
                            price=entry_price,
                            bankroll=sizer.bankroll,
                        ))
        elif decision.skip_reason and self._tick_count % 60 == 0:
            asyncio.create_task(self._persist_signal(
                state, features, decision, decision.skip_reason,
                roc_value=roc_val,
            ))
        elif (decision.conviction == Conviction.LOW
                and decision.direction is not None
                and not decision.should_trade_in(mode)
                and self._tick_count % 60 == 0):
            logger.info("coordinator.roc_low_skipped",
                        direction=decision.direction.value,
                        roc_dir=decision.roc_dir.value,
                        obi_dir=decision.obi_dir.value,
                        spread_state=decision.spread_state.value,
                        regime=regime,
                        mode=mode)

    def _register_position_ticker(self, ticker: str, symbol: str) -> None:
        """Tell the WS client to watch lifecycle events for this ticker."""
        kalshi_ws = self.data_manager._kalshi_ws
        if kalshi_ws and ticker:
            kalshi_ws.watched_position_tickers[ticker] = symbol

    def _unregister_position_ticker(self, ticker: str) -> None:
        kalshi_ws = self.data_manager._kalshi_ws
        if kalshi_ws and ticker:
            kalshi_ws.watched_position_tickers.pop(ticker, None)

    async def _handle_live_entry(self, trader, ticker, decision, entry_price,
                                  regime, features, roc_val, symbol, state) -> None:
        """Await a live trader entry (async) then run common post-entry logic.

        PositionManager's lock prevents concurrent entries/exits.
        """
        try:
            pos = await trader.enter(
                ticker=ticker,
                direction=decision.direction.value,
                price=entry_price,
                conviction=decision.conviction.value,
                regime=regime,
                obi=features.obi,
                roc=roc_val,
                signal_driver=decision.signal_driver,
            )
            if pos:
                self._on_trade_entry(pos, symbol, state, features, decision, roc_val, "live")
            else:
                await get_notifier().position_sizing_failed(
                    size_dollars=self.live_sizer.calculate_size(decision.conviction.value, decision.direction.value),
                    price=entry_price,
                    bankroll=self.live_sizer.bankroll,
                )
        except Exception as e:
            logger.error("coordinator.live_entry_failed", error=str(e))
        finally:
            self._live_entry_in_flight = False

    def _check_exits_for(self, state, features, regime: str, trader) -> Optional[str]:
        """Check exit conditions for a specific trader's position."""
        pos = trader.position
        if pos is None:
            return None

        if pos.candles_held < 2 and regime != "HIGH":
            return None

        current_price = self._get_exit_price_for(state, trader)
        if current_price is None:
            return None

        d = 1 if pos.direction == "long" else -1
        pnl_per_contract = d * (current_price - pos.entry_price) / 100
        notional = pos.contracts * pos.entry_price / 100
        pnl_pct = (pnl_per_contract * pos.contracts) / notional if notional > 0 else 0

        pos.max_favorable_excursion = max(pos.max_favorable_excursion, pnl_pct)
        pos.max_adverse_excursion = min(pos.max_adverse_excursion, pnl_pct)

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
        """Reject entries when the order book is empty or data feeds are stale."""
        ob = state.order_book
        if ob.best_yes_bid is None or ob.best_yes_ask is None:
            return False

        now = time.time()
        kalshi_ws = self.data_manager._kalshi_ws
        if kalshi_ws and kalshi_ws.last_message_time is not None:
            age = now - kalshi_ws.last_message_time
            if age > 60:
                logger.warning("coordinator.kalshi_stale", age_sec=round(age, 1))
                return False

        spot_ws = self.data_manager._spot_ws
        if spot_ws and spot_ws.last_message_time is not None:
            age = now - spot_ws.last_message_time
            if age > 60:
                logger.warning("coordinator.spot_stale", age_sec=round(age, 1))
                return False
        elif spot_ws and spot_ws.last_message_time is None:
            return False

        return True

    def _get_entry_price(self, state, direction) -> Optional[float]:
        """Get entry price: buy YES at ask for LONG, buy NO (sell YES at bid) for SHORT."""
        if direction == Direction.LONG:
            return state.order_book.best_yes_ask
        else:
            return state.order_book.best_yes_bid

    def _get_exit_price_for(self, state, trader) -> Optional[float]:
        """Get exit price based on a specific trader's position direction."""
        pos = trader.position
        if pos is None:
            return None
        mid = state.order_book.mid
        if mid is not None:
            return mid
        if pos.direction == "long":
            return state.order_book.best_yes_bid
        return state.order_book.best_yes_ask

    def _serialize_decision(self, mode: str = "paper") -> Optional[dict]:
        d = self._last_paper_decision if mode == "paper" else self._last_live_decision
        if d is None:
            return None
        return {
            "direction": d.direction.value if d.direction else None,
            "conviction": d.conviction.value,
            "obi_dir": d.obi_dir.value,
            "roc_dir": d.roc_dir.value,
            "spread_state": d.spread_state.value,
            "signal_driver": d.signal_driver,
            "skip_reason": d.skip_reason,
            "should_trade": d.should_trade_in(mode),
        }

    # ── Persistence ────────────────────────────────────────────────────

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

            for attr_mode in ("paper", "live"):
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
                            (date_str, attr_mode),
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
                               ON CONFLICT (date, trading_mode) DO UPDATE
                               SET total_trades = EXCLUDED.total_trades,
                                   total_pnl    = EXCLUDED.total_pnl,
                                   attribution  = EXCLUDED.attribution""",
                            (date_str, attr.get("total_trades", 0),
                             attr.get("total_pnl_dollars", 0),
                             json.dumps(attr), attr_mode),
                        )

                    if trades:
                        notifier = get_notifier()
                        await notifier.daily_attribution_report(date_str, attr)

                    logger.info("coordinator.daily_attribution_done",
                                date=date_str, mode=attr_mode, trades=len(trades))

                except Exception as e:
                    logger.error("coordinator.daily_attribution_failed",
                                 mode=attr_mode, error=str(e))

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

    async def _schedule_paper_sizer_resets(self) -> None:
        """Reset paper sizer daily/weekly baselines automatically.

        Daily reset at UTC midnight keeps paper risk metrics fresh on
        the dashboard. Weekly reset on Mondays. This runs regardless of
        the never_halt flag so the dashboard numbers stay meaningful.
        """
        while True:
            now_utc = datetime.now(timezone.utc)
            next_midnight = (now_utc + timedelta(days=1)).replace(
                hour=0, minute=0, second=5, microsecond=0
            )
            wait_sec = (next_midnight - now_utc).total_seconds()
            await asyncio.sleep(wait_sec)

            self.paper_sizer.reset_daily()
            logger.info("coordinator.paper_sizer_daily_reset",
                        bankroll=self.paper_sizer.bankroll)

            if datetime.now(timezone.utc).weekday() == 0:  # Monday
                self.paper_sizer.reset_weekly()
                logger.info("coordinator.paper_sizer_weekly_reset",
                            bankroll=self.paper_sizer.bankroll)

    async def _warmup_atr(self) -> None:
        """Pre-seed ATR filter from historical candles so regime and atr_pct
        are available immediately on startup instead of waiting 3.5 hours."""
        try:
            pool = self._pool
            if pool is None:
                return
            async with pool.connection() as conn:
                rows = await conn.execute(
                    """SELECT high, low, close FROM candles
                       ORDER BY timestamp DESC LIMIT 50"""
                )
                result = await rows.fetchall()
            if not result:
                logger.info("coordinator.atr_warmup_skipped", reason="no_candles")
                return
            candles = [(float(r[0]), float(r[1]), float(r[2]))
                       for r in reversed(result)]
            consumed = self.atr_filter.warmup(candles)
            state = self.atr_filter.get_state()
            logger.info("coordinator.atr_warmup_complete",
                        candles_consumed=consumed,
                        regime=state["regime"],
                        atr_pct=state["atr_pct"])
        except Exception as e:
            logger.warning("coordinator.atr_warmup_failed", error=str(e))

    async def _warmup_spread_filter(self) -> None:
        """Pre-seed SpreadRegimeFilter from recent ob_snapshots so the
        spread baseline is populated before the first live tick rather
        than needing ~20 ticks (~10 min) to warm up.
        """
        try:
            pool = self._pool
            if pool is None:
                return
            async with pool.connection() as conn:
                rows = await conn.execute(
                    """SELECT spread_cents FROM ob_snapshots
                       WHERE spread_cents IS NOT NULL
                       ORDER BY timestamp DESC LIMIT 200"""
                )
                result = await rows.fetchall()
            if not result:
                logger.info("coordinator.spread_warmup_skipped", reason="no_snapshots")
                return
            values = [float(r[0]) for r in reversed(result) if r[0] is not None]
            consumed = self.spread_filter.warmup(values)
            state = self.spread_filter.get_state()
            logger.info("coordinator.spread_warmup_complete",
                        values_consumed=consumed,
                        baseline_cents=state.get("baseline_cents"),
                        history_len=state.get("history_len", 0))
        except Exception as e:
            logger.warning("coordinator.spread_warmup_failed", error=str(e))

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

    async def _persist_trade(self, trade, mode: str = "paper") -> tuple[bool, Optional[int]]:
        """Persist trade to DB. Returns (quarantined, trade_id)."""
        try:
            pool = self._pool
            if pool is None:
                return False, None

            sizer = self.live_sizer if mode == "live" else self.paper_sizer

            error_reason = None
            if mode == "live":
                is_rapid = self._detect_rapid_fire()
                if is_rapid:
                    error_reason = "RAPID_FIRE_LOOP"
                elif trade.candles_held == 0 and trade.exit_reason == "STOP_LOSS":
                    error_reason = "INSTANT_STOP_LOSS"

            if error_reason:
                sizer.reverse_trade(trade.pnl)
                async with pool.connection() as conn:
                    await conn.execute(
                        """INSERT INTO errored_trades
                           (timestamp, ticker, direction, side, contracts, entry_price,
                            exit_price, pnl, pnl_pct, fees, exit_reason, conviction,
                            regime_at_entry, candles_held, entry_obi, entry_roc,
                            signal_driver, closed_at, error_reason, flagged_at, trading_mode)
                           VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, NOW(), %s)""",
                        (
                            trade.ticker, trade.direction,
                            "yes" if trade.direction == "long" else "no",
                            trade.contracts, trade.entry_price, trade.exit_price,
                            trade.pnl, trade.pnl_pct, trade.fees, trade.exit_reason,
                            trade.conviction, trade.regime_at_entry, trade.candles_held,
                            getattr(trade, "entry_obi", 0.0) or 0.0,
                            getattr(trade, "entry_roc", 0.0) or 0.0,
                            getattr(trade, "signal_driver", "-") or "-",
                            error_reason, mode,
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
                return True, None

            async with pool.connection() as conn:
                row = await conn.execute(
                    """INSERT INTO trades
                       (timestamp, ticker, direction, side, contracts, entry_price,
                        exit_price, pnl, pnl_pct, fees, exit_reason, conviction,
                        regime_at_entry, candles_held, entry_obi, entry_roc,
                        signal_driver, closed_at, trading_mode)
                       VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
                       RETURNING id""",
                    (
                        trade.ticker, trade.direction,
                        "yes" if trade.direction == "long" else "no",
                        trade.contracts, trade.entry_price, trade.exit_price,
                        trade.pnl, trade.pnl_pct, trade.fees, trade.exit_reason,
                        trade.conviction, trade.regime_at_entry, trade.candles_held,
                        getattr(trade, "entry_obi", 0.0) or 0.0,
                        getattr(trade, "entry_roc", 0.0) or 0.0,
                        getattr(trade, "signal_driver", "-") or "-",
                        mode,
                    ),
                )
                result = await row.fetchone()
                trade_id = result[0] if result else None
            return False, trade_id
        except Exception as e:
            logger.error("coordinator.persist_trade_failed", error=str(e))
            asyncio.create_task(get_notifier().db_error("persist_trade", str(e)))
            return False, None

    async def _persist_signal(self, state, features, decision, action: str,
                              roc_value: float = None) -> None:
        try:
            pool = self._pool
            if pool is None:
                return
            async with pool.connection() as conn:
                await conn.execute(
                    """INSERT INTO signal_log
                       (timestamp, ticker, obi_value, obi_direction, roc_value,
                        roc_direction, atr_regime, decision, conviction,
                        skip_reason, size_mult, spread_state)
                       VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        state.kalshi_ticker or state.symbol,
                        features.obi,
                        decision.obi_dir.value,
                        roc_value,
                        decision.roc_dir.value,
                        self.atr_filter.current_regime,
                        action,
                        decision.conviction.value,
                        decision.skip_reason,
                        decision.size_multiplier,
                        decision.spread_state.value,
                    ),
                )
        except Exception as e:
            logger.error("coordinator.persist_signal_failed", error=str(e))
            asyncio.create_task(get_notifier().db_error("persist_signal", str(e)))

    async def _persist_equity(self, mode: str = "paper") -> None:
        try:
            pool = self._pool
            if pool is None:
                return
            sizer = self.live_sizer if mode == "live" else self.paper_sizer
            trader = self.live_trader if mode == "live" else self.paper_trader
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
                        len(trader.trades),
                        mode,
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
                "ml_data_ready_sent": self._ml_data_ready_sent,
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
                    raw_paused = state.get("trading_paused", "off")
                    if raw_paused is True:
                        self.trading_paused = "paused"
                    elif raw_paused is False:
                        self.trading_paused = "off"
                    else:
                        self.trading_paused = raw_paused
                    saved_mode = state.get("trading_mode")
                    if saved_mode in ("paper", "live"):
                        self.trading_mode = saved_mode
                    self._ml_data_ready_sent = state.get("ml_data_ready_sent", False)
                    logger.info("coordinator.state_restored",
                                paper_bankroll=self.paper_sizer.bankroll,
                                live_bankroll=self.live_sizer.bankroll,
                                trading_mode=self.trading_mode)
                else:
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

            try:
                await self.sync_live_bankroll(is_initial=True)
            except Exception as e:
                logger.warning("coordinator.live_balance_fetch_failed", error=str(e))

            try:
                await self._cancel_stale_orders()
            except Exception as e:
                logger.warning("coordinator.cancel_stale_orders_failed", error=str(e))

            try:
                await self._reconcile_live_positions()
            except Exception as e:
                logger.warning("coordinator.reconcile_failed", error=str(e))
        except Exception as e:
            logger.warning("coordinator.restore_state_failed", error=str(e))

    async def _cancel_stale_orders(self) -> None:
        """Cancel any resting orders left from a previous session."""
        try:
            orders_data = await self.live_trader.client.get_orders(status="resting")
            orders = orders_data.get("orders", [])
            if not orders:
                return
            for order in orders:
                ticker = order.get("ticker", "")
                is_ours = any(ticker.startswith(p) for p in ("KXBTC", "KXETH"))
                if not is_ours:
                    continue
                order_id = order.get("order_id")
                if order_id:
                    try:
                        await self.live_trader.client.cancel_order(order_id)
                        logger.info("coordinator.stale_order_canceled",
                                    ticker=ticker, order_id=order_id)
                    except Exception as e:
                        logger.warning("coordinator.stale_order_cancel_failed",
                                       order_id=order_id, error=str(e))
        except Exception as e:
            logger.warning("coordinator.stale_orders_fetch_failed", error=str(e))

    async def _reconcile_live_positions(self) -> None:
        """Delegate reconciliation to PositionManager (handles locking, orphan
        adoption, ghost detection, and DESYNC state transitions)."""
        pm = self.live_trader.position_manager

        old_position_ticker = pm.position.ticker if pm.position else None

        await pm.reconcile()

        if old_position_ticker and not pm.has_position:
            self._unregister_position_ticker(old_position_ticker)
            asyncio.create_task(get_notifier().unhandled_exception(
                location="coordinator._reconcile_live_positions",
                error=f"Ghost position cleared: bot had {old_position_ticker} but exchange shows no position",
            ))
            try:
                await self.sync_live_bankroll()
            except Exception:
                pass

        new_orphan_count = len(pm.orphaned_positions)
        if new_orphan_count > 0 and new_orphan_count != getattr(self, '_last_orphan_count', 0):
            self._last_orphan_count = new_orphan_count
            orphan_details = []
            total_exposure = 0.0
            for o in pm.orphaned_positions:
                exposure = o.contracts * o.avg_entry_price / 100
                total_exposure += exposure
                orphan_details.append(f"{o.ticker} ({o.direction}, {o.contracts}x @ {o.avg_entry_price}c = ${exposure:.2f})")
            logger.warning("coordinator.orphans_detected",
                           count=new_orphan_count,
                           tickers=[o.ticker for o in pm.orphaned_positions],
                           total_exposure=round(total_exposure, 2))
            asyncio.create_task(get_notifier().unhandled_exception(
                location="coordinator._reconcile_live_positions",
                error=(
                    f"Detected {new_orphan_count} orphaned positions "
                    f"(${total_exposure:.2f} exposure): {'; '.join(orphan_details)}"
                ),
            ))
        elif new_orphan_count == 0 and getattr(self, '_last_orphan_count', 0) > 0:
            self._last_orphan_count = 0

    async def _periodic_reconciliation(self) -> None:
        """Periodic check: detect exchange positions the bot doesn't know about,
        and sync wallet balance to keep internal bankroll accurate."""
        try:
            await self._reconcile_live_positions()
        except Exception as e:
            logger.warning("coordinator.periodic_reconcile_failed", error=str(e))

        try:
            await self.sync_live_bankroll()
        except Exception as e:
            logger.warning("coordinator.periodic_wallet_sync_failed", error=str(e))


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
