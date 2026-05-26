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
import os
import signal
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import psutil
import structlog

from config import settings
from data.manager import DataManager
from data.candle_aggregator import CandleAggregator
from features.engine import FeatureEngine
from strategies.obi import evaluate_obi, check_obi_exit, Direction
from strategies.roc import evaluate_roc, calculate_roc, check_roc_exit
from strategies.resolver import SignalConflictResolver, Conviction
from strategies.spread_div import evaluate_spread_divergence, SpreadState
from strategies.exit_intelligence import (
    HealthComponents,
    compute_position_health_score,
)
from filters.atr_regime import ATRRegimeFilter
from filters.spread_regime import SpreadRegimeFilter
from risk.position_sizer import PositionSizer
from risk.circuit_breaker import CircuitBreaker
from execution.paper_trader import PaperTrader
from execution.live_trader import LiveTrader
from data.fill_stream import FillStream
from api.ws import ws_manager
from database import get_pool, close_pool
from database.connection import write_gate
from notifications import get_notifier
from filters.price_guard import PriceGuard
from filters.trend_guard import TrendGuard
from filters.edge_profile import evaluate as evaluate_edge_profile, evaluate_paper as evaluate_paper_edge
from ml.feature_capture import extract_features, save_features, label_trade
from ml.inference import ml_gate
from data.historical_sync import HistoricalSync
from monitoring.live_health import record_pipeline_health, run_live_health_checks

logger = structlog.get_logger(__name__)


def _bg_persist_max_env_default() -> int:
    """Read BG_PERSIST_MAX from env with a 96 default.

    2026-05-04 (BUG-032 follow-up #2): empirically a higher cap (256) made
    things *worse* by letting the queue accumulate enough write-blocked
    tasks (each pinning a copy of MarketState / OrderBook references) to
    push container memory over the 1.8 GB limit and trigger SIGKILL inside
    ~2 minutes of uptime. The real fix is in ``_persist_snapshot`` (we now
    serialize the order book to JSON synchronously in the calling task so
    the queued coroutine holds only small primitives) — combined with this
    smaller cap, RSS now stays under ~600 MiB at steady state. 96 is large
    enough to absorb cold-start / ticker-rotation bursts but small enough
    that even the worst-case per-task memory footprint can't OOM us.
    """
    import os
    raw = os.getenv("BG_PERSIST_MAX")
    if raw is None:
        return 96
    try:
        v = int(raw)
        return v if v > 0 else 96
    except ValueError:
        return 96


# 2026-05-05 (BUG-035): parse the ticker-encoded close time so we can tell
# whether a restored / orphaned position belongs to a contract that has
# already settled. The Kalshi format is reliable enough for this purpose:
#   KX{SYMBOL}-{YY}{MMM}{DD}{HH}-{B|T}{strike}
# e.g. ``KXBTC-26MAY0510-B81350`` closes at 10:00 *ET* on 2026-05-05,
# which is 14:00 UTC during EDT or 15:00 UTC during EST.
#
# 2026-05-06 (BUG-035 follow-up): the encoded HOUR is in **Eastern Time**,
# not UTC. The earlier UTC interpretation made every ticker appear 4-5
# hours earlier than its actual Kalshi ``close_time``, which caused the
# entry-side guard to reject all valid markets for the entire window
# (4-5 hours per session) and the paper-side watchdog to prematurely
# force-close 9 paper positions on 2026-05-06 (-$130 net synthetic loss).
# Use ``zoneinfo("America/New_York")`` so DST handoffs are automatic.
# Returns ``None`` for any ticker that doesn't match the expected shape so
# callers can fall back to "treat as live" instead of mis-parsing into a
# false stale verdict.
import re as _re_ticker_close
from functools import lru_cache
try:
    from zoneinfo import ZoneInfo as _ZoneInfo  # py>=3.9 stdlib
    _ET_TZ = _ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - fallback if tzdata missing
    _ET_TZ = timezone(timedelta(hours=-5))  # conservative EST fallback

_TICKER_CLOSE_RE = _re_ticker_close.compile(
    r"^KX[A-Z]+-(\d{2})([A-Z]{3})(\d{2})(\d{2})-[BT].+$"
)
_MONTH_ABBR_TO_NUM = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


# 2026-05-06 (BUG-035 follow-up #2): the parser was being called on
# *every* market tick from two hot paths -- the paper-position watchdog
# in ``_run_settlement_guards`` and the entry-side stale-ticker guard in
# ``_evaluate_entry_for``. py-spy showed it eating ~20% of MainThread
# CPU after the BUG-035 fixes shipped (regex match + str.upper() +
# ZoneInfo datetime + .astimezone() per call), pushing the loop over
# budget so the Coinbase WS couldn't service its keepalive ping. The
# function is pure -- same ticker string in, same UTC datetime out --
# so an LRU cache is correct. Tickers are bounded (96/day per symbol)
# so 256 entries is comfortably oversized; older entries are auto-evicted.
@lru_cache(maxsize=256)
def _ticker_close_time(ticker: str) -> Optional[datetime]:
    """Parse the close time from a Kalshi ticker string. Returns None on any
    parse failure (unknown format, bad month abbrev, invalid date).

    The encoded hour is **Eastern Time** (with DST). Returned datetime is
    always normalised to UTC so downstream comparisons against
    ``datetime.now(timezone.utc)`` are correct year-round.

    Cached: same ticker string always yields the same close time, so the
    LRU cache (size 256) keeps this O(1) on the hot per-tick paths.
    """
    if not ticker:
        return None
    m = _TICKER_CLOSE_RE.match(ticker.upper())
    if not m:
        return None
    yy, mmm, dd, hh = m.groups()
    month = _MONTH_ABBR_TO_NUM.get(mmm)
    if month is None:
        return None
    try:
        et_dt = datetime(
            year=2000 + int(yy),
            month=month,
            day=int(dd),
            hour=int(hh),
            minute=0,
            second=0,
            tzinfo=_ET_TZ,
        )
    except ValueError:
        return None
    return et_dt.astimezone(timezone.utc)


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

        # BUG-025: shared FillStream subscriber. Constructed eagerly so
        # snapshot/restore and reconciliation logic always see a non-None
        # value, but ``start()`` is what actually opens the WebSocket --
        # which only runs when ``settings.live.use_fill_stream`` is True.
        self.fill_stream: Optional[FillStream] = (
            FillStream() if settings.live.use_fill_stream else None
        )

        self.paper_trader = PaperTrader(self.paper_sizer)
        self.live_trader = LiveTrader(self.live_sizer, fill_stream=self.fill_stream)

        self.trading_mode = settings.bot.trading_mode
        self.trading_paused = "off"  # "off" | "settling" | "paused"
        self.param_overrides: dict = {}
        self._pool = None
        self._tick_count = 0
        self._last_paper_decision = None
        self._last_live_decision = None
        self._last_paper_exit_tick = -999
        self._last_live_exit_tick = -999
        # BUG-035 (2026-05-05): wall-clock companion to ``_last_paper_exit_tick``.
        # The tick-based gate (``ticks_since_exit > 100``) was ~3s at 30Hz
        # tick rate, which left a HARD_STOP_LOSS rapid-fire loop free to
        # re-enter immediately on the next favourable ask. The wall-clock
        # cooldown is bounded by ``settings.bot.paper_reentry_cooldown_sec``
        # (default 5s) so the book has time to breathe.
        self._last_paper_exit_wall_ts: float = 0.0
        # BUG-036 (2026-05-06): per-(ticker, direction) cooldown for paper
        # re-entries. The 5s wall-clock cooldown above is global ("don't
        # re-enter ANY paper trade for 5s") which the 17:04-17:05 incident
        # showed was insufficient — the bot lost the same KXBTC long 7
        # times in 86s because OBI stayed pinned bullish on the same
        # ticker every 8-30s. We now also require ``paper_same_side_cooldown_sec``
        # (default 60s) between exits and re-entries on the same
        # (ticker, direction) tuple. Keys are tuples; values are the
        # wall-clock ts of the last exit on that pair.
        self._last_paper_exit_per_pair: dict[tuple[str, str], float] = {}
        self._last_regime: Optional[str] = None
        self._cb_was_halted = False
        self._recent_exit_times: list[float] = []
        self._rapid_fire_count = 0
        self._orphan_check_in_flight = False

        # Fix 1: Duplicate entry guard — prevents concurrent live entry tasks
        self._live_entry_in_flight = False
        self._live_exit_in_flight = False

        # 2026-05-01 OOM fix: bounded background-task set for high-frequency
        # fire-and-forget persists (snapshots, signals, equity, save_state).
        # Without a cap, slow DB writes caused create_task() coros to pile up
        # holding references to state/features/decision until the container
        # got OOM-killed (~1.5 GiB → restart loop). When the queue is full we
        # *drop* the new task instead of letting memory grow unbounded — the
        # loss of a single ob_snapshot is harmless; an OOM kill is not.
        #
        # 2026-05-04 (BUG-032 follow-up #2): default 96. The earlier 256
        # bump made things worse (queued coroutines pinned MarketState
        # references → SIGKILL in ~2 min). Combined with the snapshot
        # eager-serialization fix in ``_persist_snapshot`` and the 5 Hz
        # broadcast throttle below, 96 gives the cold start enough
        # headroom without letting in-flight tasks dominate RSS.
        # Configurable via env so we can tune in prod without a redeploy.
        self._bg_persist_tasks: set = set()
        self._bg_persist_max = _bg_persist_max_env_default()
        self._bg_persist_dropped = 0
        # 2026-05-04 (BUG-032 follow-up #2): wall-clock throttle for the
        # per-tick dashboard broadcast. The dashboard renders at ~2-3 fps;
        # broadcasting every tick (up to ~30 Hz during ticker rotation)
        # was queueing serialized snapshots that the dashboard would just
        # collapse + drop client-side anyway, while pinning RSS in the
        # bg-persist queue. 1 Hz is plenty for a human dashboard and
        # keeps the event loop from saturating on JSON serialisation.
        self._last_broadcast_ts: float = 0.0
        self._broadcast_min_interval_sec: float = 1.0  # 1 Hz

        # 2026-05-04 (BUG-032 follow-up): generic rate-limit cache for
        # noisy per-tick log lines. A sustained signal during the
        # high-frequency tick stream after a ticker rotation can fire
        # 40+ identical log lines in 500ms and CPU-bind the event loop
        # to the point where healthchecks time out and the container
        # restart-loops. Keyed by a tuple unique to the log site +
        # variant; we re-log on a 30s wall-clock cadence so the
        # condition is still observable on dashboards / alerts.
        # Currently used by: ``coordinator.tfi_downgrade``,
        # ``coordinator.price_guard_rejected``.
        self._tfi_downgrade_log_cache: dict = {}
        # 2026-05-04 (BUG-032 follow-up): single-flight guard for
        # ``_periodic_reconciliation``. Without it the tick loop can
        # spawn a fresh reconcile every ``reconcile_interval`` ticks
        # before the previous one finishes (each reconcile does a REST
        # round-trip, can take 10+ seconds under network jitter).
        # Concurrent reconciles compound CPU load and double-fire the
        # post-reconcile bankroll syncs that we observed flooding logs
        # at 3-per-32ms during the 2026-05-04 incident.
        self._periodic_reconcile_in_flight: bool = False

        # BUG-028 telemetry: counts how many ticks were skipped because the
        # active contract was within `min_seconds_to_expiry` (or its
        # remaining time was unknown). Surfaced on /api/diagnostics so we
        # can spot rotation-window regressions without grepping logs.
        self._near_expiry_skip_count: dict[str, int] = {"paper": 0, "live": 0}

        # ML feature snapshots: keyed by ticker, captured at entry, consumed at exit
        self._pending_features: dict[str, dict] = {}

        # Exit-intelligence runtime telemetry caches.
        self._health_breach_counts: dict[str, int] = {}
        self._last_health_snapshot: dict[str, Optional[dict[str, Any]]] = {
            "paper": None,
            "live": None,
        }
        self._last_position_telemetry_ts: dict[str, float] = {}

        # One-time alert: fires when 500+ fully-labeled paper trades exist
        self._ml_data_ready_sent: bool = False
        # One-time alert: fires when paper telemetry coverage is rich enough
        # to evaluate promoting health-score from shadow → enforced exit.
        self._exit_intel_promotion_sent: bool = False

        # Throttle for periodic Kalshi balance polling. Event-driven syncs
        # (post-trade exit, /api/reset-drawdown, ghost cleared, startup,
        # toggle-to-live) ignore this and always go to the wire; only the
        # tick-driven `_periodic_reconciliation` poll honors it. Without
        # this the bot was hitting /portfolio/balance ~30x/min, flooding
        # logs and wasting Kalshi rate budget while flat.
        self._last_bankroll_sync_ts: float = 0.0
        self._bankroll_sync_min_interval_sec: float = 30.0

        self._rss_watchdog_task: Optional[asyncio.Task] = None
        self._last_watchdog_tracemalloc_ts: float = 0.0

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

    async def sync_live_bankroll(self, is_initial: bool = False,
                                  force: bool = True) -> float:
        """Fetch real Kalshi wallet balance and update live_sizer.

        The Kalshi wallet is the source of truth. Peak bankroll tracks the
        high-water mark from real trading, but is capped to the wallet on
        sync so that external losses (orphaned trades, manual withdrawals)
        don't cause a permanent drawdown halt.

        daily/weekly baselines are only set on initial sync (startup) or
        when explicitly requested — not on every sync call.

        force=False short-circuits to the cached value if we synced within
        ``_bankroll_sync_min_interval_sec``. Used by the periodic poll only;
        all event-driven callers (post-trade, reset-drawdown, ghost cleared,
        startup, toggle) leave force=True so they always see the freshest
        balance.
        """
        now = time.time()
        if (
            not force
            and not is_initial
            and (now - self._last_bankroll_sync_ts) < self._bankroll_sync_min_interval_sec
        ):
            return self.live_sizer.bankroll

        balance_data = await self.live_trader.client.get_balance()
        wallet = float(balance_data.get("balance", 0)) / 100
        self._last_bankroll_sync_ts = time.time()
        if wallet > 0:
            old_wallet = self.live_sizer.bankroll
            self.live_sizer.bankroll = wallet
            old_peak = self.live_sizer.peak_bankroll
            if old_peak == settings.bot.initial_bankroll:
                self.live_sizer.peak_bankroll = wallet
            else:
                self.live_sizer.peak_bankroll = max(wallet, old_peak)
            if is_initial:
                self.live_sizer.daily_start_bankroll = wallet
                self.live_sizer.weekly_start_bankroll = wallet
            # 2026-05-04 BUG-032 follow-up: only log at INFO when the
            # wallet ACTUALLY changed, the call was forced (event-driven
            # post-trade / startup), or this is the initial sync. The
            # periodic poll at force=False used to log even on no-op
            # calls, contributing to the spammy log stream that was
            # CPU-binding the event loop.
            if is_initial or force or abs(wallet - old_wallet) > 0.005:
                logger.info("coordinator.live_bankroll_synced",
                            wallet=wallet, peak=self.live_sizer.peak_bankroll)
        return wallet

    async def start(self):
        self._pool = await get_pool()
        self.live_trader.position_manager.set_db_pool(self._pool)
        await self.live_trader.position_manager.restore_state()
        await self._restore_state()
        await self._restore_paper_position()
        await self._warmup_atr()
        await self._warmup_spread_filter()
        self.data_manager.add_listener(self._on_market_update)
        await self.data_manager.start()
        # BUG-025: kick off the authenticated fill subscriber. Failure
        # to connect is non-fatal -- PositionManager falls back to the
        # polled order response transparently.
        if self.fill_stream is not None:
            try:
                await self.fill_stream.start()
            except Exception as e:
                logger.warning("coordinator.fill_stream_start_failed", error=str(e))
        asyncio.create_task(self._schedule_tuning())
        asyncio.create_task(self._schedule_daily_attribution())
        asyncio.create_task(self._schedule_weekly_digest())
        asyncio.create_task(self._schedule_paper_sizer_resets())
        asyncio.create_task(self._schedule_live_health())
        if settings.bot.rss_watchdog_enabled:
            if not tracemalloc.is_tracing():
                tracemalloc.start(25)
            self._rss_watchdog_task = asyncio.create_task(self._rss_watchdog_loop())
        await self.historical_sync.start(self._pool)
        logger.info("coordinator.started",
                    fill_stream_enabled=self.fill_stream is not None)

    async def stop(self):
        await self._save_state()
        if self._rss_watchdog_task is not None:
            self._rss_watchdog_task.cancel()
            try:
                await self._rss_watchdog_task
            except asyncio.CancelledError:
                pass
            self._rss_watchdog_task = None
        if self.fill_stream is not None:
            try:
                await self.fill_stream.stop()
            except Exception:
                pass
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
                self._spawn_bg_persist(self._persist_candle(symbol, completed_candle))

            old_regime = self.atr_filter.current_regime
            self.atr_filter.update(
                completed_candle.high,
                completed_candle.low,
                completed_candle.close,
            )
            new_regime = self.atr_filter.current_regime

            if self.paper_trader.has_position:
                self.paper_trader.position.candles_held += 1
                self._spawn_bg_persist(self._save_paper_position())
            if self.live_trader.has_position:
                self.live_trader.position.candles_held += 1
                self._spawn_bg_persist(self.live_trader.position_manager._persist_state())

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
        # 2026-05-04 (BUG-032 follow-up #2): throttle to 5 Hz wall-clock.
        # Was firing every tick (~30 Hz during ticker rotations) and the
        # serialized payload was the largest single contributor to the
        # bg-persist queue's memory footprint (state + features dicts
        # held until ws send completes). 5 Hz is fast enough for a human
        # dashboard but flat-lines the queue under tick storms.
        now_t = time.time()
        if (now_t - self._last_broadcast_ts) >= self._broadcast_min_interval_sec:
            self._last_broadcast_ts = now_t
            self._spawn_bg_persist(
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
        # 2026-05-01 OOM fix: snapshot persists were happening every 10 ticks
        # (~1 Hz) which, under DB write_gate pressure, caused create_task
        # coroutines to back up holding state/features references → OOM.
        # 30-tick interval is still fine for backtest replay granularity.
        # 2026-05-04 (BUG-032 follow-up #2): serialize the order book
        # synchronously here so the queued task only carries small
        # primitives, not a live MarketState reference. This dropped per-
        # task footprint from ~few KB (with full order book pinned via
        # __closure__) to a fixed ~few hundred bytes.
        if self._tick_count % 30 == 0 and self._pool is not None:
            import json as _json
            ticker = state.kalshi_ticker or symbol
            bids_json = _json.dumps([list(p) for p in state.order_book.top_n_bids(10)])
            asks_json = _json.dumps([list(p) for p in state.order_book.top_n_asks(10)])
            self._spawn_bg_persist(self._persist_snapshot_eager(
                ticker, bids_json, asks_json,
                features.obi, features.total_bid_vol,
                features.total_ask_vol, features.spread_cents,
            ))

        high_risk_window = (
            regime == "HIGH"
            or (state.time_remaining_sec is not None and state.time_remaining_sec < 300)
        )
        reconcile_interval = 15 if high_risk_window else 50

        if self._tick_count % reconcile_interval == 0 and self.live_trader.orphaned_positions and not self._orphan_check_in_flight:
            self._orphan_check_in_flight = True
            asyncio.create_task(self._check_orphaned_positions())

        if self._tick_count % 60 == 0 and self._pool is not None:
            self._spawn_bg_persist(self._persist_equity("paper"))
            if self.live_enabled:
                self._spawn_bg_persist(self._persist_equity("live"))
        if self._tick_count % 300 == 0 and self._pool is not None:
            self._spawn_bg_persist(self._save_state())

        if (self._tick_count % reconcile_interval == 0
                and self.live_enabled
                and not self.live_trader.position_manager.is_busy
                and not self._periodic_reconcile_in_flight):
            self._periodic_reconcile_in_flight = True
            asyncio.create_task(self._periodic_reconciliation_wrapper())

    # ── Settlement guards ──────────────────────────────────────────────

    def _run_settlement_guards(self, symbol: str, state, trader, mode: str) -> None:
        """Handle settlement and expiry guard for a given trader lane."""
        is_live = mode == "live"
        pm_busy = is_live and self.live_trader.position_manager.is_busy

        # 2026-05-05 (BUG-035): "outlived contract" watchdog. Settlement
        # below requires ``pos.ticker == state.kalshi_ticker``, but the
        # data layer rotates ``state.kalshi_ticker`` to the new active
        # contract on every tick (manager._notify, line ~355) -- so a
        # ``lifecycle_settled`` event for the *old* contract races with
        # the rotation and is overwritten before this guard can match.
        # When that happens the position becomes permanently un-settleable
        # and the EXPIRY_GUARD branch below spams ``skip_no_liquidity``
        # at ~23 Hz forever, saturating the event loop.
        #
        # Fail-safe: parse the position's ticker-encoded close_time. Once
        # the contract has been closed for a small grace window (gives the
        # normal settlement path a chance to land first) and we still hold
        # the position, force-close it as a no-PnL synthetic settlement so
        # the lane unblocks. Live lane is handled by the position manager's
        # orphan/reconciliation paths -- this watchdog is paper-only.
        if (not is_live and trader.has_position and not pm_busy):
            pos = trader.position
            close_time = _ticker_close_time(pos.ticker) if pos else None
            if close_time is not None:
                age_sec = (datetime.now(timezone.utc) - close_time).total_seconds()
                if age_sec > settings.bot.stale_paper_grace_sec:
                    logger.warning(
                        "coordinator.paper_position_stale_cleanup",
                        ticker=pos.ticker,
                        contract_close_utc=close_time.isoformat(),
                        closed_seconds_ago=int(age_sec),
                        direction=pos.direction,
                        contracts=pos.contracts,
                        entry_price=pos.entry_price,
                        mode=mode,
                    )
                    # Exit at entry price (zero PnL before fees) so attribution
                    # isn't polluted by a fictional outcome. Tag with a
                    # dedicated reason so dashboards / attribution can filter.
                    trade = trader.exit(
                        pos.entry_price,
                        "STALE_TICKER_CLEANUP",
                        fill_source="paper_stale_cleanup",
                    )
                    if trade:
                        self._on_trade_exit(trade, symbol, mode)
                    return

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
                if is_live:
                    current_price = self._get_exit_price_for(state, trader)
                    if current_price is not None and current_price > pos.entry_price:
                        logger.info("coordinator.short_settlement_guard",
                                    ticker=pos.ticker, entry=pos.entry_price,
                                    current=current_price, remaining_sec=state.time_remaining_sec,
                                    mode=mode)
                        if not self._live_exit_in_flight:
                            self._live_exit_in_flight = True
                            asyncio.create_task(self._handle_live_exit(
                                asyncio.ensure_future(trader.exit(current_price, "SHORT_SETTLEMENT_GUARD")),
                                symbol,
                                original_reason="SHORT_SETTLEMENT_GUARD",
                                exit_price=current_price,
                            ))
                else:
                    # Paper guards must use the executable side to avoid
                    # inflating PnL with mid-price synthetic fills when the
                    # book is one-sided near close. If the executable side
                    # is missing, decline the guard fill -- settlement will
                    # close the trade through the normal settlement path.
                    exec_price = self._get_executable_exit_price_for(state, trader)
                    if exec_price is None:
                        # BUG-035: rate-limit to 30s/key. The 2026-05-05
                        # incident saw the EXPIRY_GUARD sibling spam ~23
                        # lines/sec for hours when a paper position
                        # outlived its contract; the watchdog above now
                        # cleans those up but the rate-limit is defense
                        # in depth against any future tight loop.
                        log_key = ("paper_short_guard_skip", pos.ticker, mode)
                        last_logged = self._tfi_downgrade_log_cache.get(log_key)
                        now_t = time.time()
                        if last_logged is None or (now_t - last_logged) > 30.0:
                            self._tfi_downgrade_log_cache[log_key] = now_t
                            logger.info("coordinator.short_settlement_guard.skip_no_liquidity",
                                        ticker=pos.ticker, entry=pos.entry_price,
                                        remaining_sec=state.time_remaining_sec, mode=mode)
                    elif exec_price > pos.entry_price:
                        logger.info("coordinator.short_settlement_guard",
                                    ticker=pos.ticker, entry=pos.entry_price,
                                    current=exec_price, remaining_sec=state.time_remaining_sec,
                                    mode=mode, fill_source="paper_guard_taker_bidask")
                        trade = trader.exit(
                            exec_price,
                            "SHORT_SETTLEMENT_GUARD",
                            fill_source="paper_guard_taker_bidask",
                        )
                        if trade:
                            self._on_trade_exit(trade, symbol, mode)

            # Phase 3 (Expiry Exit Reliability, 2026-05-04): pre-expiry
            # passive limit ladder. Feature-flagged per mode; default OFF.
            # Runs in the window [expiry_guard_trigger_sec, ladder_start_trigger_sec]
            # so the ladder ALWAYS yields to EXPIRY_GUARD before the close.
            # Live ladder is invoked via the lock-acquiring ``try_passive_limit_ladder``
            # method; paper trades skip the ladder entirely (paper exits
            # are deterministic and the ladder buys nothing in simulation).
            ladder_start = settings.bot.ladder_start_trigger_sec
            ladder_enabled = (
                settings.bot.ladder_enabled_live if is_live
                else settings.bot.ladder_enabled_paper
            )
            if (is_live and ladder_enabled and pos
                    and state.time_remaining_sec is not None
                    and state.time_remaining_sec < ladder_start
                    and state.time_remaining_sec >= settings.bot.expiry_guard_trigger_sec
                    and not pm_busy and not self._live_exit_in_flight):
                self._live_exit_in_flight = True
                asyncio.create_task(self._run_pre_expiry_ladder(
                    symbol, state,
                ))

            # BUG-032: trigger EXPIRY_GUARD at T-``expiry_guard_trigger_sec``
            # (default 180s), not the historical T-60s. A single failed
            # exit attempt can burn 18+ seconds during pre-close volatility,
            # and the coordinator's retry sequence (2s + 4s backoff + 3x
            # ~22s requests) needs the full window to complete before the
            # contract closes. Without this widened buffer we hit 409
            # Conflict on every retry and orphan the position.
            expiry_trigger = settings.bot.expiry_guard_trigger_sec
            if (pos and state.time_remaining_sec is not None
                    and state.time_remaining_sec < expiry_trigger):
                if is_live:
                    # Live retains the entry-price fallback because the
                    # exchange order will be aggressively re-priced inside
                    # _exit_inner. We only need a non-None marker here.
                    exit_price = self._get_exit_price_for(state, trader) or pos.entry_price
                    if not pm_busy and not self._live_exit_in_flight:
                        self._live_exit_in_flight = True
                        asyncio.create_task(self._handle_live_exit(
                            asyncio.ensure_future(trader.exit(exit_price, "EXPIRY_GUARD")),
                            symbol,
                            original_reason="EXPIRY_GUARD",
                            exit_price=exit_price,
                        ))
                else:
                    # Paper EXPIRY_GUARD must reflect realistic taker exit
                    # prices. If executable side is missing, decline the
                    # synthetic fill and let settlement resolve the trade.
                    exec_price = self._get_executable_exit_price_for(state, trader)
                    if exec_price is None:
                        # BUG-035: rate-limit to 30s/key. Pre-fix, this log
                        # spammed ~23 lines/sec for ~7.5h when a paper
                        # position outlived its contract -- driving CPU
                        # to 100%, breaking websocket keepalives, and
                        # starving the DB pool. The watchdog at the top
                        # of this function now cleans up the root cause;
                        # this rate-limit prevents any future tight loop
                        # in the same code path from melting the bot.
                        log_key = ("paper_expiry_guard_skip", pos.ticker, mode)
                        last_logged = self._tfi_downgrade_log_cache.get(log_key)
                        now_t = time.time()
                        if last_logged is None or (now_t - last_logged) > 30.0:
                            self._tfi_downgrade_log_cache[log_key] = now_t
                            logger.info("coordinator.expiry_guard.skip_no_liquidity",
                                        ticker=pos.ticker, entry=pos.entry_price,
                                        remaining_sec=state.time_remaining_sec, mode=mode)
                    else:
                        trade = trader.exit(
                            exec_price,
                            "EXPIRY_GUARD",
                            fill_source="paper_guard_taker_bidask",
                        )
                        if trade:
                            self._on_trade_exit(trade, symbol, mode)

    # ── Paper lane ─────────────────────────────────────────────────────

    def _run_paper_lane(self, symbol: str, state, features, regime: str) -> None:
        trader = self.paper_trader
        sizer = self.paper_sizer
        breaker = self.paper_breaker

        if trader.has_position:
            exit_reason = self._check_exits_for(
                state, features, regime, trader, mode="paper"
            )
            if exit_reason:
                exit_price = self._get_exit_price_for(state, trader)
                if exit_price is not None:
                    trade = trader.exit(exit_price, exit_reason)
                    if trade:
                        self._on_trade_exit(trade, symbol, "paper")

        # BUG-028: refuse entry when the time-to-expiry is unknown or below
        # the configured threshold. Previous version (`time_remaining_sec is
        # not None and ... < 120`) let `None` pass, which is exactly the
        # state we observed every time an EXPIRY_409_SETTLED fired: ticker
        # rotated but no `ticker` WS event had populated `expiry_time` for
        # the new contract yet. With this guard, a missing remaining-time
        # value is treated as too-close-to-expiry rather than as
        # signal-quality-clean.
        near_expiry = self._is_near_expiry(state.time_remaining_sec)
        if not trader.has_position and not near_expiry:
            ticks_since_exit = self._tick_count - self._last_paper_exit_tick
            book_healthy = self._is_book_healthy(state)
            # BUG-035 (2026-05-05): wall-clock cooldown in addition to
            # the tick-count gate. The 100-tick gate is only ~3s at
            # 30Hz which left a HARD_STOP_LOSS rapid-fire loop free to
            # re-enter on the next favourable ask; the resulting trade
            # storm pinned CPU at 100%. Default 5s; tunable via
            # PAPER_REENTRY_COOLDOWN_SEC.
            now_t = time.time()
            cooldown_ok = (now_t - self._last_paper_exit_wall_ts
                           >= settings.bot.paper_reentry_cooldown_sec)
            if ticks_since_exit > 100 and book_healthy and cooldown_ok:
                self._evaluate_entry_for(
                    symbol, state, features, regime,
                    trader, sizer, breaker, "paper",
                )
        elif not trader.has_position and self._tick_count % 60 == 0:
            self._log_near_expiry_skip(state, "paper")

    # ── Live lane ──────────────────────────────────────────────────────

    def _run_live_lane(self, symbol: str, state, features, regime: str) -> None:
        trader = self.live_trader
        sizer = self.live_sizer
        breaker = self.live_breaker
        pm = trader.position_manager

        if trader.has_position and not pm.is_busy and not self._live_exit_in_flight:
            exit_reason = self._check_exits_for(
                state, features, regime, trader, mode="live"
            )
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

        # BUG-028: see _run_paper_lane for rationale on the None-case fix.
        near_expiry = self._is_near_expiry(state.time_remaining_sec)
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
        elif (pm.can_enter
                and self.trading_paused == "off"
                and not self._live_entry_in_flight
                and self._tick_count % 60 == 0):
            self._log_near_expiry_skip(state, "live")

    # ── Trade exit / entry callbacks ───────────────────────────────────

    def _on_trade_exit(self, trade, symbol: str, mode: str = "paper") -> None:
        """Common post-exit logic for both paper and live trades."""
        if mode == "live":
            self._unregister_position_ticker(trade.ticker)
            self._last_live_exit_tick = self._tick_count

            # Supervised single-trade mode: when enabled, auto-pause after
            # every live trade so the operator must review before the next
            # one. Default OFF — operators rely on the ``live_trade_limit``
            # cap and manual dashboard pauses instead. Toggle with
            # SUPERVISED_AUTO_PAUSE=true in .env and restart.
            if settings.bot.supervised_auto_pause:
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
            # BUG-035: wall-clock cooldown so HARD_STOP_LOSS rapid-fire
            # loops can't whip the bot into 100% CPU.
            now_ts = time.time()
            self._last_paper_exit_wall_ts = now_ts
            # BUG-036: also remember per-(ticker, direction) so the
            # entry guard can refuse re-entry on the same losing setup.
            ticker = getattr(trade, "ticker", "") or ""
            direction = getattr(trade, "direction", "") or ""
            if ticker and direction:
                self._last_paper_exit_per_pair[(ticker, direction)] = now_ts

        position_uid = getattr(trade, "position_uid", "") or ""
        if position_uid:
            key = f"{mode}:{position_uid}"
            self._health_breach_counts.pop(key, None)
            self._last_position_telemetry_ts.pop(key, None)
        self._last_health_snapshot[mode] = None

        asyncio.create_task(self._persist_and_notify_exit(trade, symbol, mode))

    async def _persist_and_notify_exit(self, trade, symbol: str, mode: str) -> None:
        """Persist trade first, then notify. Skip Discord if trade was quarantined."""
        quarantined, trade_id = await self._persist_trade(trade, mode)

        if mode == "paper":
            await self._clear_paper_position()

        if trade_id is not None:
            await self._save_and_label_features(trade, trade_id, mode)

        if mode == "paper" and not self._ml_data_ready_sent:
            asyncio.create_task(self._check_ml_data_threshold())

        if (
            mode == "paper"
            and not self._exit_intel_promotion_sent
            and settings.bot.exit_intelligence_enabled
            and settings.bot.exit_intelligence_shadow_only
            and settings.bot.position_telemetry_enabled
        ):
            asyncio.create_task(self._check_exit_intelligence_promotion_threshold())

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

    async def _check_exit_intelligence_promotion_threshold(self) -> None:
        """One-time check: fire a Discord alert when paper telemetry coverage
        is rich enough to evaluate promoting the health-score from shadow
        mode to an enforced exit driver.

        Criteria (all must be met):
          * ``EXIT_INTEL_PROMOTION_MIN_PAPER_TRADES`` completed paper trades
            in the last 30 days that have at least one ``position_telemetry``
            row with a non-null ``health_score`` inside their
            entry-to-exit window.
          * ``EXIT_INTEL_PROMOTION_MIN_REGIMES`` distinct ATR regimes at
            entry across that cohort (default 2 — typically LOW + MEDIUM).
          * ``EXIT_INTEL_PROMOTION_MIN_HOURS`` distinct UTC entry hours
            (default 6 — guards against single-session bias).

        Win/loss min-score split is included in the alert so the operator
        can immediately see whether the gate is informative.
        """
        try:
            pool = self._pool
            if pool is None:
                return
            min_trades = int(settings.bot.exit_intel_promotion_min_paper_trades)
            min_regimes = int(settings.bot.exit_intel_promotion_min_distinct_regimes)
            min_hours = int(settings.bot.exit_intel_promotion_min_distinct_hours)

            async with pool.connection() as conn:
                row = await conn.execute(
                    """
                    -- Joins trades to position_telemetry by ``position_uid``.
                    -- Time-window joins are unsafe because trades.timestamp is
                    -- written at close time (== closed_at), not entry time —
                    -- see migration 013_trade_position_uid.sql.
                    -- Hour-of-day diversity is derived from ``closed_at`` as a
                    -- close-enough proxy (worst case: trade entered late in
                    -- one hour and exited just into the next; immaterial for
                    -- the ``>= 6 distinct hours`` gate).
                    WITH trade_telemetry AS (
                        SELECT
                            t.id AS trade_id,
                            t.pnl,
                            t.regime_at_entry,
                            EXTRACT(HOUR FROM t.closed_at AT TIME ZONE 'UTC')::int AS hour_utc,
                            MIN(pt.health_score) AS min_health_score,
                            COUNT(pt.id) AS sample_count
                        FROM trades t
                        LEFT JOIN position_telemetry pt
                          ON pt.position_uid = t.position_uid
                         AND pt.trading_mode = 'paper'
                         AND pt.health_score IS NOT NULL
                        WHERE t.trading_mode = 'paper'
                          AND t.timestamp > NOW() - INTERVAL '30 days'
                          AND t.closed_at IS NOT NULL
                          AND t.position_uid IS NOT NULL
                        GROUP BY t.id, t.pnl, t.regime_at_entry, t.closed_at
                    )
                    SELECT
                        COUNT(*) FILTER (WHERE sample_count > 0)                                          AS qualifying_trades,
                        COUNT(DISTINCT regime_at_entry) FILTER (WHERE sample_count > 0)                   AS distinct_regimes,
                        COUNT(DISTINCT hour_utc) FILTER (WHERE sample_count > 0)                          AS distinct_hours,
                        COUNT(*) FILTER (WHERE sample_count > 0 AND pnl > 0)                              AS winners_with_telemetry,
                        COUNT(*) FILTER (WHERE sample_count > 0 AND pnl <= 0)                             AS losers_with_telemetry,
                        AVG(min_health_score) FILTER (WHERE sample_count > 0 AND pnl > 0)                 AS avg_min_score_winners,
                        AVG(min_health_score) FILTER (WHERE sample_count > 0 AND pnl <= 0)                AS avg_min_score_losers
                    FROM trade_telemetry
                    """
                )
                result = await row.fetchone()

            if result is None:
                return

            (
                qualifying_trades,
                distinct_regimes,
                distinct_hours,
                winners_with_telemetry,
                losers_with_telemetry,
                avg_min_score_winners,
                avg_min_score_losers,
            ) = (
                int(result[0] or 0),
                int(result[1] or 0),
                int(result[2] or 0),
                int(result[3] or 0),
                int(result[4] or 0),
                float(result[5]) if result[5] is not None else None,
                float(result[6]) if result[6] is not None else None,
            )

            if (
                qualifying_trades < min_trades
                or distinct_regimes < min_regimes
                or distinct_hours < min_hours
            ):
                logger.debug(
                    "coordinator.exit_intel_promotion_pending",
                    qualifying_trades=qualifying_trades,
                    distinct_regimes=distinct_regimes,
                    distinct_hours=distinct_hours,
                    min_trades=min_trades,
                    min_regimes=min_regimes,
                    min_hours=min_hours,
                )
                return

            self._exit_intel_promotion_sent = True
            await self._save_state()

            await get_notifier().exit_intelligence_promotion_ready(
                qualifying_trades=qualifying_trades,
                distinct_regimes=distinct_regimes,
                distinct_hours=distinct_hours,
                winners_with_telemetry=winners_with_telemetry,
                losers_with_telemetry=losers_with_telemetry,
                avg_min_score_winners=avg_min_score_winners,
                avg_min_score_losers=avg_min_score_losers,
                current_threshold=float(settings.bot.health_score_threshold),
                breach_ticks=int(settings.bot.health_score_breach_ticks),
            )
            logger.info(
                "coordinator.exit_intel_promotion_alert_sent",
                qualifying_trades=qualifying_trades,
                distinct_regimes=distinct_regimes,
                distinct_hours=distinct_hours,
                winners=winners_with_telemetry,
                losers=losers_with_telemetry,
                avg_min_score_winners=avg_min_score_winners,
                avg_min_score_losers=avg_min_score_losers,
            )
        except Exception as e:
            logger.warning("coordinator.exit_intel_promotion_check_failed", error=str(e))

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
            if trade.exit_reason not in (
                "CONTRACT_SETTLED",
                "CONTRACT_SETTLED_VERIFY_FAILED",
                "EXPIRY_409_SETTLED",
            ):
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
        else:
            asyncio.create_task(self._save_paper_position())
        position_uid = getattr(pos, "position_uid", "") or ""
        if position_uid:
            key = f"{mode}:{position_uid}"
            self._health_breach_counts[key] = 0
            self._last_position_telemetry_ts[key] = 0.0

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
                "position_uid": position_uid,
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

    async def _run_pre_expiry_ladder(self, symbol: str, state) -> None:
        """Phase 3: drive the pre-expiry passive limit ladder for the
        live position. Always falls back to a normal trade-exit
        completion path so partial-or-no-fill simply lets EXPIRY_GUARD
        run on the next tick.

        The ``_live_exit_in_flight`` flag is set by the caller and
        cleared here to ensure no double-fire even if the ladder
        produces a partial residual.
        """
        try:
            pos = self.live_trader.position
            if pos is None:
                return
            ob = self._get_order_book_for_ticker(state, pos.ticker)
            if ob is None:
                logger.info(
                    "coordinator.pre_expiry_ladder_no_book",
                    ticker=pos.ticker,
                )
                return
            try:
                result = await self.live_trader.position_manager.try_passive_limit_ladder(
                    best_yes_bid=ob.best_yes_bid,
                    best_yes_ask=ob.best_yes_ask,
                    time_remaining_sec=state.time_remaining_sec,
                )
            except Exception as e:
                logger.warning(
                    "coordinator.pre_expiry_ladder_failed",
                    error=str(e), ticker=getattr(pos, "ticker", None),
                )
                return

            if result is None:
                logger.info(
                    "coordinator.pre_expiry_ladder_no_fill",
                    ticker=getattr(pos, "ticker", None),
                )
                return

            try:
                trade = self.live_trader._build_trade(result)
                self.live_trader.sizer.record_trade(trade.pnl)
                self.live_trader.trades.append(trade)
                self._on_trade_exit(trade, symbol, "live")
            except Exception as e:
                logger.error(
                    "coordinator.pre_expiry_ladder_post_fill_failed",
                    error=str(e),
                )
        finally:
            self._live_exit_in_flight = False

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

        Phase 2 (Expiry Exit Reliability, 2026-05-04): retries pass
        ``attempt`` (1-based for the retry loop, mapped to the
        position manager's 0-based ``attempt`` arg) so EXPIRY_GUARD
        / SHORT_SETTLEMENT_GUARD retries can use the widening
        order-side floor schedule. Backoff and max attempts are now
        configurable via ``BotConfig.expiry_retry_*``; existing
        defaults preserve the legacy 2s + 4s sequence.
        """
        try:
            trade = await trade_future
            if trade:
                self._on_trade_exit(trade, symbol, "live")
                return

            if not self.live_trader.has_position:
                return

            retry_price = exit_price or self.live_trader.position.entry_price

            cfg = settings.bot
            # Total attempts INCLUDING the original attempt that just
            # failed. The retry loop runs (max_attempts - 1) extra
            # tries. Default 3 = original + 2 retries (legacy behavior).
            max_attempts = max(1, cfg.expiry_retry_max_attempts)
            backoff_base = max(0.0, cfg.expiry_retry_backoff_base_sec)
            max_backoff = max(backoff_base, cfg.expiry_retry_max_backoff_sec)
            for retry_num in range(1, max_attempts):
                if not self.live_trader.has_position:
                    return
                # Exponential backoff capped at max_backoff. Using
                # base ** retry_num so retry_num=1 → 2s, retry_num=2
                # → 4s with the default base 2.0 (matches the legacy
                # hardcoded schedule).
                delay = min(max_backoff, backoff_base ** retry_num) if backoff_base > 0 else 0.0
                logger.warning("coordinator.live_exit_retry",
                               attempt=retry_num, delay=delay,
                               reason=original_reason,
                               max_attempts=max_attempts)
                await asyncio.sleep(delay)
                try:
                    trade = await self.live_trader.exit(
                        retry_price, original_reason, attempt=retry_num,
                    )
                    if trade:
                        self._on_trade_exit(trade, symbol, "live")
                        return
                except Exception as e:
                    logger.error("coordinator.live_exit_retry_failed",
                                 attempt=retry_num, error=str(e))

            if self.live_trader.has_position:
                pos = self.live_trader.position
                logger.error("coordinator.live_exit_abandoned",
                             ticker=pos.ticker, contracts=pos.contracts)
                self._unregister_position_ticker(pos.ticker)
                # BUG-031 runtime fix: use the atomic adopt-and-clear so
                # state never lands in OPEN/EXITING with position=null
                # between the adopt_orphan call and the position-clear.
                # The previous two-step sequence was the runtime path
                # that re-introduced the OPEN-with-null-position snapshot
                # we'd already fixed at restore time.
                self.live_trader.position_manager.adopt_orphan_and_clear_position(
                    ticker=pos.ticker,
                    direction=pos.direction,
                    contracts=pos.contracts,
                    avg_entry_price=pos.entry_price,
                )
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
            new_round_trips = 0
            for info in closed:
                pnl = info["pnl"]
                notional = info["contracts"] * info["entry_price"] / 100
                pnl_pct = pnl / notional if notional > 0 else 0
                fees = notional * self.live_trader.FEE_RATE

                logger.info("coordinator.orphan_recovered",
                            ticker=info["ticker"], pnl=pnl,
                            reason=info["reason"],
                            already_counted=info.get("already_counted", False))

                if await self._is_duplicate_orphan_trade(info["ticker"], info["reason"]):
                    continue

                # Fix C: every orphan that closes here ended a real live
                # round-trip and must move the supervised counter — unless
                # the settlement path already counted it (already_counted).
                if not info.get("already_counted", False):
                    new_round_trips += 1

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

            # Fix C: advance the supervised round-trip counter once we've
            # finished processing this batch. The position manager owns the
            # counter, but only the coordinator knows which orphans were
            # newly settled here (vs already counted by the 409 settlement
            # path). Bump in a single call so the persisted snapshot only
            # writes once per check.
            if new_round_trips > 0:
                self.live_trader.position_manager.bump_completed_trades(
                    new_round_trips, source="orphan_recovery",
                )

            remaining = len(self.live_trader.orphaned_positions)
            if closed:
                logger.info("coordinator.orphan_check_complete",
                            closed=len(closed), remaining=remaining,
                            counter_bumped=new_round_trips)
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
        # BUG-035 (2026-05-05) entry-side hardening: reject any active
        # ticker whose ticker-encoded close_time is in the past. This
        # closes a feedback loop where ``kalshi_ws._resolve_tickers`` can
        # transiently surface a just-closed contract as "active" (e.g.
        # during the gap between expiring contracts overnight, or while a
        # fresh contract is still being created server-side). Without this
        # guard the bot enters → BUG-035 watchdog cleans up → entry path
        # immediately retries → spin loop. We log at WARNING with a 30s
        # rate limit so a long stale window is observable but not noisy.
        active_ticker = state.kalshi_ticker if hasattr(state, "kalshi_ticker") else None
        close_time = _ticker_close_time(active_ticker) if active_ticker else None
        if close_time is not None and close_time <= datetime.now(timezone.utc):
            log_key = ("stale_active_ticker_skip", active_ticker, mode)
            last_logged = self._tfi_downgrade_log_cache.get(log_key)
            now_t = time.time()
            if last_logged is None or (now_t - last_logged) > 30.0:
                self._tfi_downgrade_log_cache[log_key] = now_t
                logger.warning(
                    "coordinator.entry_skip_stale_active_ticker",
                    ticker=active_ticker,
                    contract_close_utc=close_time.isoformat(),
                    closed_seconds_ago=int((datetime.now(timezone.utc) - close_time).total_seconds()),
                    mode=mode,
                )
            return

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
                    # 2026-05-04 (BUG-032 follow-up): rate-limit the
                    # tfi_downgrade log to once per (ticker, mode, obi_dir,
                    # new_conviction) combo. Without this, a sustained
                    # OBI/TFI disagreement during the high-frequency tick
                    # stream after a ticker rotation can fire 40+ identical
                    # log lines in 500ms and tip the bot into a CPU-bound
                    # restart loop. The log only carries diagnostic value
                    # on transitions, not on every tick that the condition
                    # holds.
                    log_key = (ticker, mode, decision.obi_dir.value,
                               old_conv.value, new_conv.value)
                    last_logged = self._tfi_downgrade_log_cache.get(log_key)
                    now_t = time.time()
                    if last_logged is None or (now_t - last_logged) > 30.0:
                        self._tfi_downgrade_log_cache[log_key] = now_t
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

        # roc_val computed earlier (above the edge_profile gate) and reused
        # here so the filter and the trade record see the same value.

        self.trend_guard.apply_short_trend_filter(decision, closes, mode)

        # ML gate — final pre-entry filter. Runs AFTER all signal/filter logic.
        # Fail-open: if no model is loaded (no .pkl on disk), ml_gate returns
        # (True, 0.5) and the trade proceeds unchanged.
        ml_cfg = settings.ml
        if (ml_cfg.gate_enabled
                and decision.should_trade_in(mode)
                and ((mode == "paper" and ml_cfg.gate_paper)
                     or (mode == "live" and ml_cfg.gate_live))):
            feat = extract_features(
                features=features,
                candle_aggregator=self.candle_aggregator,
                atr_filter=self.atr_filter,
                state=state,
                historical_sync=self.historical_sync,
            )
            allowed, p_win = ml_gate(feat)
            if not allowed:
                pre_dir = decision.direction.value if decision.direction else None
                decision = decision.with_conviction(
                    Conviction.NONE,
                    skip_reason=f"ML_GATE_REJECTED_p{p_win:.2f}",
                )
                logger.info("coordinator.ml_gate_rejected",
                            p_win=round(p_win, 3), mode=mode, direction=pre_dir)

        # Compute raw 5-bar ROC once and reuse for the edge_profile gate
        # (below) and downstream entry/log paths. Was previously recomputed
        # at lines ~1156 / ~1230; consolidating here removes drift between
        # what the filter sees and what we persist on the entry.
        roc_val = calculate_roc(closes, settings.roc.lookback) or 0.0

        # Edge profile gate — LIVE LANE ONLY. Restricts live trading to the
        # subset of setups validated by paper-trading attribution. Paper is
        # never affected so it keeps generating training data.
        if (mode == "live"
                and settings.edge_profile.enabled
                and decision.should_trade_in(mode)):
            edge_ok, edge_reason = evaluate_edge_profile(
                decision=decision, entry_price=None,
                roc_value=roc_val,
            )
            if not edge_ok:
                pre_dir = decision.direction.value if decision.direction else None
                decision = decision.with_conviction(
                    Conviction.NONE, skip_reason=edge_reason,
                )
                # 2026-05-04 BUG-032 follow-up: rate-limit (was logging
                # every tick during sustained signals).
                log_key = ("edge_pre", mode, pre_dir, edge_reason)
                last_logged = self._tfi_downgrade_log_cache.get(log_key)
                now_t = time.time()
                if last_logged is None or (now_t - last_logged) > 30.0:
                    self._tfi_downgrade_log_cache[log_key] = now_t
                    logger.info("coordinator.edge_profile_rejected",
                                reason=edge_reason, mode=mode, direction=pre_dir,
                                driver=decision.signal_driver)

        # Paper-lane edge gate (P1/P2 fix, 2026-05-26).
        # When EDGE_PAPER_LONG_ONLY=true and/or EDGE_PAPER_ALLOWED_DRIVERS is
        # set, the paper lane mirrors the live edge profile so paper PnL is a
        # realistic proxy for what the live bot would earn.  By default both
        # flags are unset and this block is a no-op (paper collects full-
        # strategy data for ML training).
        if (
            mode == "paper"
            and decision.should_trade_in(mode)
            and (
                settings.edge_profile.paper_long_only
                or settings.edge_profile.paper_allowed_drivers
                or settings.edge_profile.paper_blocked_hours_utc
            )
        ):
            paper_ok, paper_reason = evaluate_paper_edge(
                decision=decision,
                now_utc=datetime.fromtimestamp(time.time(), tz=timezone.utc),
            )
            if not paper_ok:
                pre_dir = decision.direction.value if decision.direction else None
                decision = decision.with_conviction(
                    Conviction.NONE, skip_reason=paper_reason,
                )
                log_key = ("paper_edge", mode, pre_dir, paper_reason)
                last_logged = self._tfi_downgrade_log_cache.get(log_key)
                now_t = time.time()
                if last_logged is None or (now_t - last_logged) > 30.0:
                    self._tfi_downgrade_log_cache[log_key] = now_t
                    logger.info(
                        "coordinator.paper_edge_rejected",
                        reason=paper_reason,
                        mode=mode,
                        direction=pre_dir,
                        driver=decision.signal_driver,
                    )

        # BUG-036 (2026-05-06): per-(ticker, direction) re-entry cooldown.
        # The 2026-05-06 incident saw the bot lose the same long on
        # KXBTC-26MAY0614-B81650 seven times in 86 seconds because the
        # global 5s wall-clock cooldown didn't prevent re-entering the
        # same losing setup once OBI re-pinned bullish on the same book.
        # Once HARD_STOP_LOSS was the dominant exit reason, this turned a
        # single losing thesis into 7 realised losses. Gate paper
        # re-entries on the same (ticker, direction) tuple by a longer
        # wall-clock cooldown (default 60s == one full ATR cycle on the
        # 15-min binary). Live lane is supervised and uses live_trade_limit
        # so we don't add the same gate there.
        if (mode == "paper"
                and decision.should_trade_in(mode)
                and decision.direction is not None):
            ticker = getattr(state, "kalshi_ticker", "") or ""
            direction = decision.direction.value.strip().lower()
            (
                same_thesis_allowed,
                lock_age_sec,
                same_side_cooldown_sec,
                unlocked_by_flip,
                unlocked_by_expiry,
            ) = self._paper_same_thesis_gate(ticker, direction)
            if unlocked_by_flip:
                log_key = ("paper_thesis_flip_unlock", ticker, direction)
                last_logged = self._tfi_downgrade_log_cache.get(log_key)
                now_t = time.time()
                if last_logged is None or (now_t - last_logged) > 30.0:
                    self._tfi_downgrade_log_cache[log_key] = now_t
                    logger.info(
                        "coordinator.paper_thesis_lock_released_flip",
                        ticker=ticker,
                        direction=direction,
                        mode=mode,
                    )
            if unlocked_by_expiry:
                log_key = ("paper_thesis_expiry_unlock", ticker, direction)
                last_logged = self._tfi_downgrade_log_cache.get(log_key)
                now_t = time.time()
                if last_logged is None or (now_t - last_logged) > 30.0:
                    self._tfi_downgrade_log_cache[log_key] = now_t
                    logger.info(
                        "coordinator.paper_thesis_lock_released_expiry",
                        ticker=ticker,
                        direction=direction,
                        cooldown_sec=same_side_cooldown_sec,
                        mode=mode,
                    )
            if not same_thesis_allowed:
                log_key = ("paper_same_side_cooldown",
                           ticker, direction)
                last_logged = self._tfi_downgrade_log_cache.get(log_key)
                now_t = time.time()
                if last_logged is None or (now_t - last_logged) > 30.0:
                    self._tfi_downgrade_log_cache[log_key] = now_t
                    logger.info(
                        "coordinator.paper_same_side_cooldown_skip",
                        ticker=ticker,
                        direction=direction,
                        seconds_since_exit=round(lock_age_sec, 1),
                        cooldown_sec=same_side_cooldown_sec,
                        mode=mode,
                    )
                return

        if decision.should_trade_in(mode):
            entry_price = self._get_entry_price(state, decision.direction)
            if entry_price is not None and entry_price > 0:
                allowed, guard_reason = self.price_guard.is_allowed(
                    entry_price, decision.direction.value,
                    regime, state.time_remaining_sec,
                )
                if not allowed:
                    # 2026-05-04 BUG-032 follow-up: rate-limit by
                    # (mode, direction, guard_reason) tuple over 30s
                    # wall-clock time. The previous tick-count-based
                    # rate limit failed catastrophically when the WS
                    # was delivering 800+ ticks/sec during a ticker
                    # rotation: ``_tick_count % 60 == 0`` aligned 4x
                    # in 280ms, contributing to the CPU-bind that
                    # triggered the post-deploy restart loop. Wall-
                    # clock rate limit is robust to tick-rate spikes.
                    log_key = ("price_guard", mode,
                               decision.direction.value, guard_reason)
                    last_logged = self._tfi_downgrade_log_cache.get(log_key)
                    now_t = time.time()
                    if last_logged is None or (now_t - last_logged) > 30.0:
                        self._tfi_downgrade_log_cache[log_key] = now_t
                        logger.info("coordinator.price_guard_rejected",
                                    price=entry_price, direction=decision.direction.value,
                                    reason=guard_reason, mode=mode)
                    return

                # Edge profile price cap (LIVE only) — checked here because
                # entry_price is finally known. The pre-filter above already
                # rejected wrong direction / driver / hour for cheap signals.
                if mode == "live" and settings.edge_profile.enabled:
                    edge_ok, edge_reason = evaluate_edge_profile(
                        decision=decision, entry_price=entry_price,
                        roc_value=roc_val,
                    )
                    if not edge_ok:
                        # 2026-05-04 BUG-032 follow-up: rate-limit log.
                        # The DB write throttle below already capped the
                        # signal_log row rate but the log line itself
                        # was still firing every tick.
                        log_key = ("edge_priced", mode,
                                   decision.direction.value, edge_reason)
                        last_logged = self._tfi_downgrade_log_cache.get(log_key)
                        now_t = time.time()
                        if last_logged is None or (now_t - last_logged) > 30.0:
                            self._tfi_downgrade_log_cache[log_key] = now_t
                            logger.info("coordinator.edge_profile_rejected",
                                        reason=edge_reason, mode=mode,
                                        price=entry_price,
                                        direction=decision.direction.value,
                                        driver=decision.signal_driver)
                        # BUG-029: rate-limit DB writes for high-frequency
                        # rejections. During momentum surges this path could
                        # fire 60+ times/sec, queueing 60 DB tasks each
                        # holding a connection from the pool, exhausting it
                        # within seconds and triggering pool timeouts that
                        # cascaded into container restarts. Throttling to
                        # once every 60 ticks (~1/min) is plenty for the
                        # edge-skip-ratio alarm to detect persistent issues
                        # without filling signal_log with millisecond-spaced
                        # duplicates of the same rejection reason.
                        if self._tick_count % 60 == 0:
                            self._spawn_bg_persist(self._persist_signal(
                                state, features, decision, edge_reason,
                                roc_value=roc_val,
                            ))
                        return

                ticker = state.kalshi_ticker or symbol
                strike_distance = self._entry_strike_distance(ticker, state.spot_price)
                max_strike_distance = max(0.0, float(settings.bot.max_strike_distance_dollars))
                if strike_distance is not None and strike_distance > max_strike_distance:
                    logger.info(
                        "coordinator.entry_skipped_otm",
                        mode=mode,
                        ticker=ticker,
                        strike=self._parse_b_band_strike(ticker),
                        spot_price=state.spot_price,
                        distance=round(strike_distance, 4),
                        max_distance=max_strike_distance,
                    )
                    if self._tick_count % 60 == 0:
                        self._spawn_bg_persist(self._persist_signal(
                            state,
                            features,
                            decision,
                            "MAX_STRIKE_DISTANCE_BLOCK",
                            roc_value=roc_val,
                        ))
                    return

                if mode == "live":
                    # BUG-022 follow-up: don't even spin up the entry task
                    # if the position manager would refuse this ticker (e.g.
                    # because it's still in the post-phantom cooldown
                    # window). Avoids burning the lock on a no-op.
                    pm = self.live_trader.position_manager
                    if not pm.can_enter_ticker(ticker):
                        if self._tick_count % 30 == 0:
                            logger.info(
                                "coordinator.live_entry_skipped_pm_refused",
                                ticker=ticker,
                                reason="cooldown_or_state",
                            )
                        return
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
            self._spawn_bg_persist(self._persist_signal(
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
            logger.info(
                "position_manager.position_book_subscribed",
                ticker=ticker,
                symbol=symbol,
                watched_count=len(kalshi_ws.watched_position_tickers),
            )
            if kalshi_ws._ws_is_open():
                asyncio.create_task(kalshi_ws.refresh_position_subscriptions())

    def _unregister_position_ticker(self, ticker: str) -> None:
        kalshi_ws = self.data_manager._kalshi_ws
        if kalshi_ws and ticker:
            kalshi_ws.watched_position_tickers.pop(ticker, None)
            logger.info(
                "position_manager.position_book_unsubscribed",
                ticker=ticker,
                watched_count=len(kalshi_ws.watched_position_tickers),
            )
            if kalshi_ws._ws_is_open():
                asyncio.create_task(kalshi_ws.refresh_position_subscriptions())

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

    def _position_uid_for(self, pos) -> str:
        uid = getattr(pos, "position_uid", "") or ""
        if uid:
            return str(uid)
        ticker = getattr(pos, "ticker", "UNKNOWN")
        entry_time = getattr(pos, "entry_time", "")
        fallback = f"{ticker}:{entry_time}"
        try:
            setattr(pos, "position_uid", fallback)
        except Exception:
            pass
        return fallback

    def _check_exits_for(
        self, state, features, regime: str, trader, mode: str
    ) -> Optional[str]:
        """Check exit conditions for a specific trader's position."""
        pos = trader.position
        if pos is None:
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

        closes = [c.close for c in self.candle_aggregator.recent(10)]
        current_roc = calculate_roc(closes, settings.roc.lookback)
        candle_list = self.candle_aggregator.recent(1)
        latest_candle = None
        if candle_list:
            c = candle_list[0]
            latest_candle = {
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
            }

        health_exit_reason: Optional[str] = None
        health_score: Optional[float] = None
        health_components: Optional[HealthComponents] = None
        health_breach_count = 0
        health_confirmation_required = bool(settings.bot.health_exit_confirmation_enabled)
        health_confirmation_met = not health_confirmation_required
        if settings.bot.exit_intelligence_enabled:
            health_score, health_components = compute_position_health_score(
                direction=pos.direction,
                current_obi=features.obi,
                current_roc=current_roc,
                entry_roc=pos.entry_roc,
                atr_regime=regime,
                regime_at_entry=pos.regime_at_entry,
                pnl_pct=pnl_pct,
                max_favorable_excursion=pos.max_favorable_excursion,
                mini_roc_fast=getattr(features, "spot_roc_30s", None),
                mini_roc_slow=getattr(features, "spot_roc_60s", None),
                weight_obi=settings.bot.health_weight_obi,
                weight_roc=settings.bot.health_weight_roc,
                weight_regime=settings.bot.health_weight_regime,
                weight_mfe=settings.bot.health_weight_mfe,
                weight_momentum=settings.bot.health_weight_momentum,
            )

            position_key = f"{mode}:{self._position_uid_for(pos)}"
            threshold = max(0.0, min(100.0, settings.bot.health_score_threshold))
            if health_score < threshold:
                health_breach_count = self._health_breach_counts.get(position_key, 0) + 1
            else:
                health_breach_count = 0
            self._health_breach_counts[position_key] = health_breach_count

            breach_ticks_required = max(1, settings.bot.health_score_breach_ticks)
            if health_breach_count >= breach_ticks_required:
                health_confirmation_met = self._health_exit_confirmation_met(
                    pos=pos,
                    current_obi=features.obi,
                    current_roc=current_roc,
                )
            self._last_health_snapshot[mode] = {
                "position_uid": self._position_uid_for(pos),
                "ticker": pos.ticker,
                "direction": pos.direction,
                "score": health_score,
                "threshold": threshold,
                "breach_count": health_breach_count,
                "breach_ticks_required": breach_ticks_required,
                "shadow_only": settings.bot.exit_intelligence_shadow_only,
                "confirmation_required": health_confirmation_required,
                "confirmation_met": health_confirmation_met,
                "components": (
                    health_components.to_dict() if health_components else None
                ),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if health_breach_count >= breach_ticks_required:
                if settings.bot.exit_intelligence_shadow_only:
                    if health_breach_count == breach_ticks_required:
                        logger.info(
                            "coordinator.health_score_breach_shadow",
                            mode=mode,
                            ticker=pos.ticker,
                            score=health_score,
                            threshold=threshold,
                            breach_count=health_breach_count,
                        )
                else:
                    if health_confirmation_met:
                        health_exit_reason = "HEALTH_SCORE_DECAY"
                        if health_breach_count == breach_ticks_required:
                            logger.info(
                                "coordinator.health_score_exit_triggered",
                                mode=mode,
                                ticker=pos.ticker,
                                score=health_score,
                                threshold=threshold,
                                breach_count=health_breach_count,
                            )
                    elif health_breach_count == breach_ticks_required:
                        logger.info(
                            "coordinator.health_score_breach_waiting_confirmation",
                            mode=mode,
                            ticker=pos.ticker,
                            score=health_score,
                            threshold=threshold,
                            breach_count=health_breach_count,
                            current_obi=features.obi,
                            current_roc=current_roc,
                            entry_obi=pos.entry_obi,
                            entry_roc=pos.entry_roc,
                        )
        else:
            self._last_health_snapshot[mode] = None

        self._maybe_persist_position_telemetry(
            mode=mode,
            pos=pos,
            state=state,
            features=features,
            regime=regime,
            mark_price=current_price,
            pnl_pct=pnl_pct,
            current_roc=current_roc,
            health_score=health_score,
            health_components=health_components,
            health_breach_count=health_breach_count,
        )

        hard_stop_pct = max(0.0, float(settings.risk.hard_stop_loss_pct))
        if hard_stop_pct > 0.0 and pnl_pct <= -hard_stop_pct:
            logger.warning(
                "coordinator.hard_stop_loss_triggered",
                mode=mode,
                ticker=pos.ticker,
                pnl_pct=round(pnl_pct, 6),
                threshold=hard_stop_pct,
                candles_held=pos.candles_held,
                regime=regime,
            )
            return "HARD_STOP_LOSS"

        min_candles_before_early_exit = max(
            0, int(settings.risk.min_candles_before_early_exit)
        )
        if pos.candles_held < min_candles_before_early_exit and regime != "HIGH":
            return health_exit_reason

        exit_reason = check_obi_exit(
            direction=pos.direction,
            current_obi=features.obi,
            pnl_pct=pnl_pct,
            candles_held=pos.candles_held,
            atr_regime=regime,
        )
        if exit_reason:
            return exit_reason

        exit_reason = check_roc_exit(
            direction=pos.direction,
            pnl_pct=pnl_pct,
            entry_roc=pos.entry_roc,
            current_roc=current_roc,
            latest_candle=latest_candle,
            candles_held=pos.candles_held,
        )
        if exit_reason:
            return exit_reason
        return health_exit_reason

    def _maybe_persist_position_telemetry(
        self,
        *,
        mode: str,
        pos,
        state,
        features,
        regime: str,
        mark_price: Optional[float],
        pnl_pct: float,
        current_roc: Optional[float],
        health_score: Optional[float],
        health_components: Optional[HealthComponents],
        health_breach_count: int,
    ) -> None:
        if not settings.bot.position_telemetry_enabled:
            return
        if self._pool is None:
            return

        position_uid = self._position_uid_for(pos)
        cache_key = f"{mode}:{position_uid}"
        now_ts = time.time()
        interval_sec = max(1.0, float(settings.bot.position_telemetry_interval_sec))
        last_ts = self._last_position_telemetry_ts.get(cache_key, 0.0)
        if now_ts - last_ts < interval_sec:
            return

        self._last_position_telemetry_ts[cache_key] = now_ts
        self._spawn_bg_persist(
            self._persist_position_telemetry(
                position_uid=position_uid,
                trading_mode=mode,
                ticker=pos.ticker,
                direction=pos.direction,
                mark_price=mark_price,
                unrealized_pnl_pct=pnl_pct,
                mfe_pct=pos.max_favorable_excursion,
                mae_pct=pos.max_adverse_excursion,
                health_score=health_score,
                health_breach_count=health_breach_count,
                obi=features.obi,
                roc_15m=current_roc,
                mini_roc_fast=getattr(features, "spot_roc_30s", None),
                mini_roc_slow=getattr(features, "spot_roc_60s", None),
                atr_regime=regime,
                time_remaining_sec=getattr(state, "time_remaining_sec", None),
                spot_price=getattr(state, "spot_price", None),
                health_components=(
                    health_components.to_dict() if health_components else None
                ),
            )
        )

    def _is_near_expiry(self, time_remaining_sec: Optional[int]) -> bool:
        """BUG-028: True when the contract is within the configured expiry
        buffer or the remaining time is unknown.

        The ``None`` case is the one that produced every observed
        EXPIRY_409_SETTLED trade: ``state.expiry_time`` had not been
        populated yet because no ``ticker`` WS event had arrived for the
        active ticker. The previous guard (``time_remaining_sec is not
        None and ... < 120``) silently passed in that case, so the entry
        path proceeded against a contract whose actual close time was
        unknown to the bot. Treating ``None`` as ``near_expiry`` is the
        safer default -- we cannot prove the contract has time to round-
        trip in, so we abstain.
        """
        threshold = settings.bot.min_seconds_to_expiry
        if time_remaining_sec is None:
            return True
        return time_remaining_sec < threshold

    def _log_near_expiry_skip(self, state, mode: str) -> None:
        """Telemetry helper for BUG-028: emit a structured log line and
        bump the diagnostics counter on a 1-per-60-tick cadence so the
        skip is visible without spamming. Persistence to ``signal_log``
        happens via the regular ``_persist_signal`` path inside
        ``_evaluate_entry_for``; we don't write a row here because we
        haven't computed any features (the goal is to be cheap on every
        tick during the close window, not to amplify load)."""
        self._near_expiry_skip_count[mode] = (
            self._near_expiry_skip_count.get(mode, 0) + 1
        )
        ticker = getattr(state, "kalshi_ticker", None)
        logger.info(
            "coordinator.entry_skipped_near_expiry",
            mode=mode,
            ticker=ticker,
            time_remaining_sec=state.time_remaining_sec,
            threshold_sec=settings.bot.min_seconds_to_expiry,
            cumulative_skips=self._near_expiry_skip_count[mode],
        )

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
                # BUG-035: rate-limit at 30s/key. Pre-fix, when the WS
                # connection died this fired ~14/sec from every entry
                # evaluation tick, contributing to the same CPU saturation
                # the rest of BUG-035 was designed to prevent.
                last_logged = self._tfi_downgrade_log_cache.get("kalshi_stale")
                if last_logged is None or (now - last_logged) > 30.0:
                    self._tfi_downgrade_log_cache["kalshi_stale"] = now
                    logger.warning("coordinator.kalshi_stale", age_sec=round(age, 1))
                return False

        spot_ws = self.data_manager._spot_ws
        if spot_ws and spot_ws.last_message_time is not None:
            age = now - spot_ws.last_message_time
            if age > 60:
                # Same rate-limit treatment as kalshi_stale above.
                last_logged = self._tfi_downgrade_log_cache.get("spot_stale")
                if last_logged is None or (now - last_logged) > 30.0:
                    self._tfi_downgrade_log_cache["spot_stale"] = now
                    logger.warning("coordinator.spot_stale", age_sec=round(age, 1))
                return False
        elif spot_ws and spot_ws.last_message_time is None:
            return False

        return True

    @staticmethod
    def _parse_b_band_strike(ticker: str) -> Optional[float]:
        """Extract strike from KX* ``-B<strike>`` contracts."""
        if "-B" not in ticker:
            return None
        try:
            raw = ticker.split("-B", 1)[1].replace(",", "")
            return float(raw)
        except (ValueError, IndexError):
            return None

    def _entry_strike_distance(self, ticker: str, spot_price: Optional[float]) -> Optional[float]:
        if spot_price is None:
            return None
        strike = self._parse_b_band_strike(ticker)
        if strike is None:
            return None
        return abs(strike - spot_price)

    @staticmethod
    def _opposite_direction(direction: str) -> Optional[str]:
        d = (direction or "").strip().lower()
        if d == "long":
            return "short"
        if d == "short":
            return "long"
        return None

    def _paper_same_thesis_gate(self, ticker: str, direction: str) -> tuple[bool, float, float, bool, bool]:
        """
        Returns:
            (allowed, age_sec, cooldown_sec, unlocked_by_flip, unlocked_by_expiry)
        """
        cooldown_sec = max(0.0, float(settings.bot.paper_same_side_cooldown_sec))
        if (
            not ticker
            or direction not in {"long", "short"}
            or cooldown_sec <= 0.0
        ):
            return True, 0.0, cooldown_sec, False, False

        now_t = time.time()
        unlocked_by_flip = False
        if settings.bot.paper_thesis_flip_unlock_enabled:
            opposite = self._opposite_direction(direction)
            if opposite is not None:
                opposite_key = (ticker, opposite)
                if opposite_key in self._last_paper_exit_per_pair:
                    self._last_paper_exit_per_pair.pop(opposite_key, None)
                    unlocked_by_flip = True

        pair_key = (ticker, direction)
        last_pair_exit = self._last_paper_exit_per_pair.get(pair_key)
        if last_pair_exit is None:
            return True, 0.0, cooldown_sec, unlocked_by_flip, False

        age = now_t - float(last_pair_exit)
        if age >= cooldown_sec:
            self._last_paper_exit_per_pair.pop(pair_key, None)
            return True, age, cooldown_sec, unlocked_by_flip, True
        return False, age, cooldown_sec, unlocked_by_flip, False

    def _health_exit_confirmation_met(self, pos, current_obi: float, current_roc: Optional[float]) -> bool:
        if not settings.bot.health_exit_confirmation_enabled:
            return True

        roc_delta = max(0.0, float(settings.bot.health_exit_confirmation_roc_delta))
        obi_delta = max(0.0, float(settings.bot.health_exit_confirmation_obi_delta))
        neutral_obi = max(
            0.0,
            min(1.0, float(settings.bot.health_exit_confirmation_neutral_obi)),
        )
        entry_obi = float(getattr(pos, "entry_obi", neutral_obi))
        entry_roc = float(getattr(pos, "entry_roc", 0.0))
        roc_now = (
            float(current_roc)
            if isinstance(current_roc, (int, float))
            else None
        )

        if pos.direction == "long":
            roc_deteriorated = roc_now is not None and roc_now <= (entry_roc - roc_delta)
            obi_deteriorated = current_obi <= min(neutral_obi, entry_obi - obi_delta)
            return roc_deteriorated or obi_deteriorated

        roc_deteriorated = roc_now is not None and roc_now >= (entry_roc + roc_delta)
        obi_deteriorated = current_obi >= max(neutral_obi, entry_obi + obi_delta)
        return roc_deteriorated or obi_deteriorated

    @staticmethod
    def _get_order_book_for_ticker(state, ticker: str):
        order_books = getattr(state, "order_books", None)
        if isinstance(order_books, dict):
            book = order_books.get(ticker)
            if book is None:
                return None
            if book.best_yes_bid is None and book.best_yes_ask is None:
                return None
            return book
        return state.order_book

    def _get_entry_price(self, state, direction) -> Optional[float]:
        """Get entry price: buy YES at ask for LONG, buy NO (sell YES at bid) for SHORT."""
        if direction == Direction.LONG:
            return state.order_book.best_yes_ask
        else:
            return state.order_book.best_yes_bid

    def _get_exit_price_for(self, state, trader) -> Optional[float]:
        """Get exit price based on a specific trader's position direction.

        Default uses the order-book mid because mid is the unbiased
        estimator for non-guard exits where the position is being
        unwound on a normal candle. For settlement-window guards
        (EXPIRY_GUARD / SHORT_SETTLEMENT_GUARD) this overestimates
        achievable paper PnL because the book is one-sided or empty
        right before close; use ``_get_executable_exit_price_for``
        in that path instead.
        """
        pos = trader.position
        if pos is None:
            return None
        book = self._get_order_book_for_ticker(state, pos.ticker)
        if book is None:
            return None
        mid = book.mid
        if mid is not None:
            return mid
        if pos.direction == "long":
            return book.best_yes_bid
        return book.best_yes_ask

    def _get_executable_exit_price_for(self, state, trader) -> Optional[float]:
        """Reason-aware exit price for paper guards near contract close.

        Uses the executable side of the book rather than mid:
          * long  -> sell YES at best YES bid    (cross the spread)
          * short -> buy back NO at best NO bid  (== 100 - best YES ask)

        Returns None when the executable side is missing -- callers
        must NOT fall back to entry_price or any synthetic value here.
        Synthetic mid fills during the settlement window were
        producing 91% paper EXPIRY_GUARD win rates with no live
        counterpart (see findings 2026-05-04). When the executable
        side is empty in paper, do NOT record a fictional fill --
        let the contract settle through the normal settlement path.
        """
        pos = trader.position
        if pos is None:
            return None
        ob = self._get_order_book_for_ticker(state, pos.ticker)
        if ob is None:
            return None
        if pos.direction == "long":
            return ob.best_yes_bid
        ask = ob.best_yes_ask
        if ask is None:
            return None
        return 100 - ask

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

    def _spawn_bg_persist(self, coro) -> None:
        """Schedule a high-frequency fire-and-forget persist with backpressure.

        2026-05-01 OOM fix: protect the event loop from unbounded
        ``asyncio.create_task`` accumulation. When too many persists are
        already queued (DB pressure), we *close* the new coroutine and
        increment a counter rather than spawn it. This trades a tiny amount
        of telemetry for surviving the next ~10 minutes of DB latency.
        """
        if len(self._bg_persist_tasks) >= self._bg_persist_max:
            self._bg_persist_dropped += 1
            try:
                coro.close()
            except Exception:
                pass
            if self._bg_persist_dropped % 50 == 1:
                logger.warning(
                    "coordinator.bg_persist_dropped",
                    queue_size=len(self._bg_persist_tasks),
                    total_dropped=self._bg_persist_dropped,
                )
            return
        task = asyncio.create_task(coro)
        self._bg_persist_tasks.add(task)
        task.add_done_callback(self._bg_persist_tasks.discard)

    async def _persist_snapshot_eager(
        self,
        ticker: str,
        bids_json: str,
        asks_json: str,
        obi: float,
        total_bid_vol: float,
        total_ask_vol: float,
        spread_cents: float,
    ) -> None:
        """2026-05-04 (BUG-032 follow-up #2): pre-serialized snapshot writer.

        Caller serializes the order book + extracts feature scalars before
        spawning the background task, so this coroutine carries only small
        primitives. Replaces the older ``_persist_snapshot(state, features)``
        path that pinned full ``MarketState`` references in the bg-persist
        queue and pushed RSS over the container limit during ticker storms.
        """
        try:
            pool = self._pool
            if pool is None:
                return
            async with write_gate():
                async with pool.connection() as conn:
                    await conn.execute(
                        """INSERT INTO ob_snapshots
                           (timestamp, ticker, bids, asks, obi, total_bid_vol, total_ask_vol, spread_cents)
                           VALUES (NOW(), %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s)""",
                        (ticker, bids_json, asks_json, obi,
                         total_bid_vol, total_ask_vol, spread_cents),
                    )
        except Exception as e:
            logger.error("coordinator.persist_failed", error=str(e))
            asyncio.create_task(get_notifier().db_error("persist_snapshot", str(e)))

    async def _persist_snapshot(self, symbol: str, state, features) -> None:
        """Legacy snapshot writer kept for tests / external callers.

        New per-tick path uses ``_persist_snapshot_eager`` (pre-serialized).
        """
        try:
            pool = self._pool
            if pool is None:
                return

            import json as _json
            bids_json = _json.dumps([list(p) for p in state.order_book.top_n_bids(10)])
            asks_json = _json.dumps([list(p) for p in state.order_book.top_n_asks(10)])

            async with write_gate():
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

    async def _persist_position_telemetry(
        self,
        *,
        position_uid: str,
        trading_mode: str,
        ticker: str,
        direction: str,
        mark_price: Optional[float],
        unrealized_pnl_pct: float,
        mfe_pct: float,
        mae_pct: float,
        health_score: Optional[float],
        health_breach_count: int,
        obi: Optional[float],
        roc_15m: Optional[float],
        mini_roc_fast: Optional[float],
        mini_roc_slow: Optional[float],
        atr_regime: Optional[str],
        time_remaining_sec: Optional[int],
        spot_price: Optional[float],
        health_components: Optional[dict[str, float]],
    ) -> None:
        try:
            pool = self._pool
            if pool is None:
                return
            async with write_gate():
                async with pool.connection() as conn:
                    await conn.execute(
                        """INSERT INTO position_telemetry (
                               timestamp,
                               position_uid,
                               trading_mode,
                               ticker,
                               direction,
                               mark_price,
                               unrealized_pnl_pct,
                               mfe_pct,
                               mae_pct,
                               health_score,
                               health_breach_count,
                               obi,
                               roc_15m,
                               mini_roc_fast,
                               mini_roc_slow,
                               atr_regime,
                               time_remaining_sec,
                               spot_price,
                               health_components
                           ) VALUES (
                               NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, %s, %s::jsonb
                           )""",
                        (
                            position_uid,
                            trading_mode,
                            ticker,
                            direction,
                            mark_price,
                            unrealized_pnl_pct,
                            mfe_pct,
                            mae_pct,
                            health_score,
                            health_breach_count,
                            obi,
                            roc_15m,
                            mini_roc_fast,
                            mini_roc_slow,
                            atr_regime,
                            time_remaining_sec,
                            spot_price,
                            json.dumps(health_components) if health_components else None,
                        ),
                    )
        except Exception as e:
            logger.warning("coordinator.persist_position_telemetry_failed", error=str(e))

    async def _schedule_tuning(self) -> None:
        """Periodic tuning task — runs every TUNING_INTERVAL_HOURS.

        Discord noise control (2026-05-02): the previous version posted
        ``tuning_cycle_report`` to Discord on every cycle, including
        no-op cycles like "Walk-forward produced no valid windows" and
        "No parameter changes needed". On a 6h cadence with insufficient
        historical candles to fill a walk-forward window (need
        ~train_window+test_window=4000 candles, bot accumulates over
        weeks), this produced 4 identical "no valid windows" Discord
        embeds per day forever. We now:

          1. Always log the cycle outcome (full visibility in container
             logs and ``coordinator.tuning_complete`` events).
          2. Post to Discord only when there's actually something the
             operator should see — health alerts present, ``should_apply``
             true, OR the cycle is the first NON-noop after a streak of
             noops (so the operator sees recovery without the noise).
          3. Once per UTC day, post a one-line summary of the previous
             24h's cycle outcomes when the day-long sequence was 100%
             noop. Keeps observability without spamming.
        """
        interval_sec = settings.bot.tuning_interval_hours * 3600
        min_candles = 2000

        # In-memory daily summary state. Reset at UTC midnight.
        skipped_streak_day = datetime.now(timezone.utc).date()
        skipped_reasons: dict[str, int] = {}
        last_summary_date = skipped_streak_day

        while True:
            await asyncio.sleep(interval_sec)
            try:
                pool = self._pool
                if pool is None:
                    continue
                from backtesting.data_loader import (
                    load_candles_db,
                    load_contract_timelines_db,
                    load_settlement_outcomes_db,
                )
                candles = await load_candles_db(pool, symbol="BTC", source="live_spot,binance")
                if len(candles) < min_candles:
                    logger.info("coordinator.tuning_skipped", reason="insufficient_candles",
                                count=len(candles), required=min_candles)
                    skipped_reasons["insufficient_candles"] = (
                        skipped_reasons.get("insufficient_candles", 0) + 1
                    )
                    await self._maybe_post_tuning_daily_summary(
                        skipped_reasons, last_summary_date,
                    )
                    last_summary_date = datetime.now(timezone.utc).date()
                    continue

                start_ts = candles[0]["timestamp"]
                end_ts = candles[-1]["timestamp"] + 900.0
                contract_timelines = await load_contract_timelines_db(
                    pool,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    series="KXBTC",
                )
                settlement_data = await load_settlement_outcomes_db(
                    pool,
                    start_ts=start_ts - 86400.0,
                    end_ts=end_ts + 86400.0,
                    series="KXBTC",
                )
                for ticker, meta in settlement_data.items():
                    timeline = contract_timelines.get(ticker)
                    if timeline is None:
                        continue
                    timeline.close_time = meta.get("close_time")
                    timeline.result = meta.get("result")
                    timeline.expiration_value = meta.get("expiration_value")
                from backtesting.auto_tuner import run_tuning_cycle
                result = await run_tuning_cycle(
                    candles, contract_timelines, pool=pool, auto_apply=False,
                )

                health_alerts: list[str] = []
                try:
                    from monitoring.signal_health import run_signal_health_check
                    health_alerts = await run_signal_health_check(pool) or []
                except Exception as e:
                    logger.warning("coordinator.signal_health_check_failed", error=str(e))

                # Always log the result.
                logger.info("coordinator.tuning_complete",
                            consistency=result.edge_consistency,
                            sharpe=result.avg_oos_sharpe,
                            should_apply=result.should_apply,
                            reason=result.reason,
                            changes_n=len(result.changes or {}))

                # Decide whether to post to Discord.
                is_noop = (
                    not result.should_apply
                    and not health_alerts
                    and not (result.changes or {})
                )
                if is_noop:
                    skipped_reasons[result.reason or "unknown"] = (
                        skipped_reasons.get(result.reason or "unknown", 0) + 1
                    )
                    logger.info("coordinator.tuning_post_skipped",
                                reason=result.reason,
                                noop_streak=sum(skipped_reasons.values()))
                else:
                    notifier = get_notifier()
                    await notifier.tuning_cycle_report(
                        edge_consistency=result.edge_consistency,
                        avg_oos_sharpe=result.avg_oos_sharpe,
                        should_apply=result.should_apply,
                        reason=result.reason,
                        changes=result.changes,
                        health_alerts=health_alerts,
                    )
                    # Reset the noop streak on every actual post so the
                    # daily summary only fires when nothing happened.
                    skipped_reasons = {}

                await self._maybe_post_tuning_daily_summary(
                    skipped_reasons, last_summary_date,
                )
                # Roll the day pointer to today so the summary fires
                # at most once per UTC day.
                last_summary_date = datetime.now(timezone.utc).date()
            except Exception as e:
                logger.error("coordinator.tuning_failed", error=str(e))

    async def _maybe_post_tuning_daily_summary(
        self,
        skipped_reasons: dict[str, int],
        last_summary_date,
    ) -> None:
        """Post a single Discord summary at most once per UTC day when
        all of the previous day's tuning cycles were noops.

        Mutates ``skipped_reasons`` in place: empties it after a successful
        post so the next day starts clean.
        """
        today = datetime.now(timezone.utc).date()
        if today == last_summary_date:
            return
        if not skipped_reasons:
            return
        try:
            notifier = get_notifier()
            total = sum(skipped_reasons.values())
            top_reason = max(skipped_reasons.items(), key=lambda kv: kv[1])[0]
            await notifier.tuning_cycle_report(
                edge_consistency=0.0,
                avg_oos_sharpe=0.0,
                should_apply=False,
                reason=(
                    f"Daily summary: {total} no-op cycles in past 24h "
                    f"(top reason: {top_reason}). No Discord posts were "
                    f"emitted per cycle. Investigate if this persists."
                ),
                changes=None,
                health_alerts=None,
            )
            logger.info("coordinator.tuning_daily_summary_posted",
                        total_noops=total, top_reason=top_reason)
        except Exception as e:
            logger.warning("coordinator.tuning_daily_summary_failed",
                           error=str(e))
        finally:
            skipped_reasons.clear()

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
                           WHERE date >= %s AND date <= %s AND trading_mode = %s
                           ORDER BY date""",
                        (week_start.isoformat(), week_end.isoformat(), self.trading_mode),
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
                           WHERE date >= %s AND date <= %s AND trading_mode = %s""",
                        (prior_start.isoformat(), prior_end.isoformat(), self.trading_mode),
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

    async def _schedule_live_health(self) -> None:
        """Hourly live-health tripwire alarms.

        Runs the three checks in monitoring/live_health.py: drought,
        edge_skip_ratio, and direction_imbalance. Each is independent
        and persists its own cooldown in bot_state, so a restart can't
        accidentally re-fire alarms that were already in cooldown.

        First alarm run is delayed 5 minutes so the bot has a chance to
        load state and start ticking before we measure anything.
        Pipeline-health row persistence runs once immediately at startup
        so the table is never empty after restarts.
        """
        try:
            pool = self._pool
            if pool is not None:
                await record_pipeline_health(pool)
        except Exception as e:
            logger.error("coordinator.pipeline_health_bootstrap_failed", error=str(e))

        await asyncio.sleep(300)
        while True:
            try:
                pool = self._pool
                if pool is not None:
                    await run_live_health_checks(
                        pool, get_notifier(),
                        trading_mode=self.trading_mode,
                        trading_paused=self.trading_paused,
                    )
            except Exception as e:
                logger.error("coordinator.live_health_check_failed",
                             error=str(e))
            await asyncio.sleep(3600)

    @staticmethod
    def _read_cgroup_memory_limit_bytes() -> Optional[int]:
        candidates = (
            Path("/sys/fs/cgroup/memory.max"),  # cgroup v2
            Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),  # cgroup v1
        )
        for candidate in candidates:
            try:
                raw = candidate.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if not raw or raw == "max":
                return None
            try:
                value = int(raw)
            except ValueError:
                continue
            if value <= 0 or value >= (1 << 60):
                return None
            return value
        return None

    def _resolve_watchdog_memory_limit_bytes(self) -> Optional[int]:
        configured_mb = int(getattr(settings.bot, "mem_limit_mb", 0) or 0)
        configured_bytes = configured_mb * 1024 * 1024 if configured_mb > 0 else None
        cgroup_bytes = self._read_cgroup_memory_limit_bytes()
        if configured_bytes and cgroup_bytes:
            return min(configured_bytes, cgroup_bytes)
        return configured_bytes or cgroup_bytes

    async def _rss_watchdog_log_tracemalloc(self, rss_bytes: int, limit_bytes: int) -> None:
        if not tracemalloc.is_tracing():
            return
        try:
            snapshot = tracemalloc.take_snapshot()
            top_stats = snapshot.statistics("lineno")[:10]
            top_frames = [
                {
                    "frame": str(stat.traceback[0]),
                    "size_kib": round(stat.size / 1024, 2),
                    "count": stat.count,
                }
                for stat in top_stats
            ]
            current_alloc, peak_alloc = tracemalloc.get_traced_memory()
            order_book_cache_sizes: dict[str, int] = {}
            for symbol, market_state in self.data_manager.states.items():
                books = getattr(market_state, "order_books", None)
                if isinstance(books, dict):
                    order_book_cache_sizes[symbol] = len(books)
            logger.info(
                "coordinator.rss_watchdog_tracemalloc",
                rss_mib=round(rss_bytes / (1024 * 1024), 2),
                limit_mib=round(limit_bytes / (1024 * 1024), 2),
                current_alloc_mib=round(current_alloc / (1024 * 1024), 2),
                peak_alloc_mib=round(peak_alloc / (1024 * 1024), 2),
                bg_persist_queue=len(self._bg_persist_tasks),
                bg_persist_dropped=self._bg_persist_dropped,
                ws_clients=len(getattr(ws_manager, "_clients", [])),
                order_book_cache_sizes=order_book_cache_sizes,
                top_frames=top_frames,
            )
        except Exception as e:
            logger.warning("coordinator.rss_watchdog_tracemalloc_failed", error=str(e))

    async def _rss_watchdog_loop(self) -> None:
        poll_sec = max(5.0, float(settings.bot.rss_watchdog_poll_sec))
        threshold_pct = float(settings.bot.rss_watchdog_threshold_pct)
        threshold_pct = min(max(threshold_pct, 0.10), 0.99)
        tracemalloc_interval_sec = max(
            30.0, float(settings.bot.rss_watchdog_tracemalloc_interval_sec)
        )
        limit_bytes = self._resolve_watchdog_memory_limit_bytes()
        if not limit_bytes:
            logger.warning("coordinator.rss_watchdog_disabled_no_limit")
            return
        logger.info(
            "coordinator.rss_watchdog_started",
            limit_mib=round(limit_bytes / (1024 * 1024), 2),
            threshold_pct=threshold_pct,
            poll_sec=poll_sec,
        )
        process = psutil.Process()
        while True:
            try:
                rss_bytes = process.memory_info().rss
                usage_ratio = rss_bytes / limit_bytes
                now_ts = time.time()

                if (
                    now_ts - self._last_watchdog_tracemalloc_ts
                    >= tracemalloc_interval_sec
                ):
                    self._last_watchdog_tracemalloc_ts = now_ts
                    await self._rss_watchdog_log_tracemalloc(rss_bytes, limit_bytes)

                if usage_ratio >= threshold_pct:
                    logger.error(
                        "coordinator.rss_watchdog_threshold_breached",
                        rss_mib=round(rss_bytes / (1024 * 1024), 2),
                        limit_mib=round(limit_bytes / (1024 * 1024), 2),
                        usage_ratio=round(usage_ratio, 4),
                        threshold_pct=threshold_pct,
                    )
                    await self._save_state()
                    await self.live_trader.position_manager._persist_state()
                    await self._save_paper_position()
                    logger.warning(
                        "coordinator.rss_watchdog_exit_requested",
                        reason="rss_threshold",
                    )
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("coordinator.rss_watchdog_failed", error=str(e))
            await asyncio.sleep(poll_sec)

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
        """Persist trade to DB. Returns (quarantined, trade_id).

        BUG-025: For live trades we additionally read the post-exit wallet
        balance, diff against the pre-entry snapshot captured by
        ``PositionManager.enter``, and quarantine when the recorded PnL
        drifts from the actual cash movement by more than
        ``WALLET_DRIFT_QUARANTINE_DOLLARS``. After BUG-027 the recorded
        PnL is the correct cash-flow figure (``exit_cost - entry_cost
        - fees``) and ``wallet_at_entry`` is captured pre-entry, so a
        non-zero drift now indicates a real reconciliation problem
        (missed fee, ledger lag, partial settlement) rather than a
        formula bug.

        Historical drift on rows 702/716/750/788 (2026-04-20 -- 2026-04-22)
        is an EXPECTED artifact, not a current bug. Those trades were
        captured by the pre-BUG-027 code path that snapshotted
        ``wallet_at_entry`` *after* the entry order had already debited
        the wallet, so their persisted ``wallet_pnl`` reconstructs as
        ``pnl + entry_cost + entry_fees`` -- which is exactly what the
        data shows to the cent on all four rows. The
        ``scripts/backfill_pnl_bug027.py`` backfill rewrote ``pnl`` to
        the correct cash-flow value but intentionally did not touch
        ``wallet_pnl`` because the historical capture timing isn't
        recoverable from the row alone (see the script's docstring).
        Trades from 2026-04-23 12:34 onward (commit ``0ce407e``) have
        correct wallet capture and zero drift -- those are the canonical
        post-fix records. Do not re-investigate this drift; see the
        2026-04-28 Tier 0 findings doc for the full diagnosis.
        """
        WALLET_DRIFT_QUARANTINE_DOLLARS = 0.05

        try:
            pool = self._pool
            if pool is None:
                return False, None

            sizer = self.live_sizer if mode == "live" else self.paper_sizer

            # ── BUG-025 wallet-PnL reconciliation (live only) ──────────
            wallet_pnl: Optional[float] = None
            pnl_drift: Optional[float] = None
            wallet_pre = getattr(trade, "wallet_at_entry", None)
            entry_fill_source = getattr(trade, "entry_fill_source", "order_response") or "order_response"
            exit_fill_source = getattr(trade, "exit_fill_source", "order_response") or "order_response"
            # Paper trades use a new optional ``fill_source`` field (Phase 1)
            # to distinguish realistic taker guard exits from legacy
            # synthetic mid-price fills. Fall back to the legacy default
            # when the trade did not carry an explicit label.
            paper_fill_source = getattr(trade, "fill_source", None)
            entry_cost_dollars = getattr(trade, "entry_cost_dollars", None)
            exit_cost_dollars = getattr(trade, "exit_cost_dollars", None)
            if mode == "live" and wallet_pre is not None:
                try:
                    bal = await self.live_trader.client.get_balance()
                    wallet_post = float(bal.get("balance", 0)) / 100.0
                    wallet_pnl = round(wallet_post - wallet_pre, 4)
                    pnl_drift = round(abs(trade.pnl - wallet_pnl), 4)
                    logger.info(
                        "coordinator.pnl_reconciliation",
                        ticker=trade.ticker,
                        recorded_pnl=trade.pnl,
                        wallet_pnl=wallet_pnl,
                        pnl_drift=pnl_drift,
                        wallet_pre=wallet_pre,
                        wallet_post=wallet_post,
                        entry_fill_source=entry_fill_source,
                        exit_fill_source=exit_fill_source,
                    )
                except Exception as e:
                    logger.warning(
                        "coordinator.wallet_reconciliation_failed",
                        ticker=trade.ticker, error=str(e),
                    )

            error_reason = None
            if mode == "live":
                is_rapid = self._detect_rapid_fire()
                if is_rapid:
                    error_reason = "RAPID_FIRE_LOOP"
                elif trade.candles_held == 0 and trade.exit_reason == "STOP_LOSS":
                    error_reason = "INSTANT_STOP_LOSS"

            # BUG-025: even if the trade is otherwise clean, quarantine
            # when wallet drift is meaningful so attribution doesn't
            # silently absorb the error. Settlement-closed trades skip
            # this -- exit_fill_source == "settlement" means there's no
            # exit cost to compare against so drift is structural.
            drift_quarantine = (
                mode == "live"
                and pnl_drift is not None
                and pnl_drift > WALLET_DRIFT_QUARANTINE_DOLLARS
                and exit_fill_source != "settlement"
            )
            if drift_quarantine and not error_reason:
                error_reason = (
                    f"BUG-025: PnL drift ${pnl_drift:.2f} "
                    f"(recorded={trade.pnl:.2f}, wallet={wallet_pnl:.2f})"
                )

            if error_reason:
                # Drift-only quarantines do NOT reverse the trade against
                # the sizer. After BUG-027 the recorded PnL matches the
                # exchange's cash flow within rounding, so flagged drift
                # is a reconciliation signal we should investigate but
                # not a reason to overwrite the trade's effect on the
                # bankroll. Structural quarantines (rapid fire / instant
                # stop) still reverse the sizer below.
                if not drift_quarantine:
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
                if drift_quarantine:
                    logger.warning(
                        "coordinator.pnl_reconciliation_drift",
                        ticker=trade.ticker,
                        recorded_pnl=trade.pnl,
                        wallet_pnl=wallet_pnl,
                        drift=pnl_drift,
                        threshold=WALLET_DRIFT_QUARANTINE_DOLLARS,
                    )
                else:
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
                # Drift quarantines still record the trade row below so
                # attribution counts the round-trip; only the structural
                # quarantines (rapid fire, instant stop) suppress the
                # main row. Mirror the prior behavior here.
                if not drift_quarantine:
                    return True, None

            # BUG-025: persist reconciliation columns alongside the row.
            # The new schema uses ``ADD COLUMN IF NOT EXISTS`` so older
            # DBs without migration 006 will still throw -- that is OK
            # because the migration runs as part of the same deploy.
            async with pool.connection() as conn:
                # Use the actual entry time so that the parity checker and any
                # analytics that join on trades.timestamp get the open time,
                # not the close time.  closed_at still captures the close time.
                entry_time_val = getattr(trade, "entry_time", None)
                row = await conn.execute(
                    """INSERT INTO trades
                       (timestamp, ticker, direction, side, contracts, entry_price,
                        exit_price, pnl, pnl_pct, fees, exit_reason, conviction,
                        regime_at_entry, candles_held, entry_obi, entry_roc,
                        signal_driver, closed_at, trading_mode,
                        entry_cost_dollars, exit_cost_dollars,
                        wallet_pnl, pnl_drift, fill_source, position_uid)
                       VALUES (COALESCE(%s, NOW()), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s,
                               %s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (
                        entry_time_val,
                        trade.ticker, trade.direction,
                        "yes" if trade.direction == "long" else "no",
                        trade.contracts, trade.entry_price, trade.exit_price,
                        trade.pnl, trade.pnl_pct, trade.fees, trade.exit_reason,
                        trade.conviction, trade.regime_at_entry, trade.candles_held,
                        getattr(trade, "entry_obi", 0.0) or 0.0,
                        getattr(trade, "entry_roc", 0.0) or 0.0,
                        getattr(trade, "signal_driver", "-") or "-",
                        mode,
                        entry_cost_dollars, exit_cost_dollars,
                        wallet_pnl, pnl_drift,
                        # Pick the most informative source. For live trades
                        # prefer the exit leg's fill source, falling back
                        # to entry when settlement-driven. For paper
                        # trades use the explicit ``fill_source`` field
                        # added in Phase 1 if present, otherwise default
                        # to ``paper_mid_mark`` so legacy synthetic fills
                        # remain identifiable in analytics.
                        (paper_fill_source or "paper_mid_mark") if mode == "paper"
                        else (exit_fill_source if exit_fill_source != "settlement" else entry_fill_source),
                        # Exit-intelligence join key. Empty string is
                        # written as NULL so the partial index on
                        # position_uid stays compact for legacy rows.
                        (getattr(trade, "position_uid", "") or None) or None,
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
            async with write_gate():
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
            async with write_gate():
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

    async def _save_paper_position(self) -> None:
        """Persist the open paper position to bot_state so it survives restarts.

        Called from _on_trade_entry (paper branch) and once per tick when the
        position is open (so candles_held / MFE / MAE updates aren't lost on a
        crash). The row is cleared in _clear_paper_position on every paper exit.

        Without this persistence, a container restart while a paper trade was
        open would orphan the open notification on Discord ("opened, never
        closed"), the in-memory position would be lost, and a brand-new entry
        could fire on the same ticker without a closing trade ever being
        recorded. See ``_persist_and_notify_exit`` for the exit half.
        """
        try:
            pool = self._pool
            if pool is None:
                return
            pos = self.paper_trader.position
            if pos is None:
                return
            import json
            payload = {
                "ticker": pos.ticker,
                "direction": pos.direction,
                "contracts": pos.contracts,
                "entry_price": pos.entry_price,
                "entry_time": pos.entry_time.isoformat(),
                "conviction": pos.conviction,
                "regime_at_entry": pos.regime_at_entry,
                "entry_obi": pos.entry_obi,
                "entry_roc": pos.entry_roc,
                "candles_held": pos.candles_held,
                "max_favorable_excursion": pos.max_favorable_excursion,
                "max_adverse_excursion": pos.max_adverse_excursion,
                "signal_driver": pos.signal_driver,
                "position_uid": getattr(pos, "position_uid", ""),
            }
            async with write_gate():
                async with pool.connection() as conn:
                    await conn.execute(
                        """INSERT INTO bot_state (key, value, updated_at)
                           VALUES ('paper_open_position', %s::jsonb, NOW())
                           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                        (json.dumps(payload),),
                    )
        except Exception as e:
            logger.warning("coordinator.save_paper_position_failed", error=str(e))

    async def _clear_paper_position(self) -> None:
        """Remove the persisted paper open-position row after exit/settlement."""
        try:
            pool = self._pool
            if pool is None:
                return
            async with write_gate():
                async with pool.connection() as conn:
                    await conn.execute(
                        "DELETE FROM bot_state WHERE key = 'paper_open_position'"
                    )
        except Exception as e:
            logger.warning("coordinator.clear_paper_position_failed", error=str(e))

    async def _restore_paper_position(self) -> None:
        """Reconstruct the paper position from bot_state on startup.

        Best-effort: any exception is logged and the bot starts FLAT. Stale
        entries (for tickers whose close_time has already passed) are dropped
        so we don't try to manage a dead position.

        2026-05-05 (BUG-035): the staleness drop used to be promised by the
        docstring but was not implemented. The 2026-05-05 incident saw a
        paper position on ``KXBTC-26MAY0515-B81350`` (closed 18:15 UTC)
        survive a restart 2.5h *after* its contract had settled, then sit
        unmanaged for another 5h while the EXPIRY_GUARD branch of
        ``_run_settlement_guards`` spammed ``skip_no_liquidity`` ~23×/sec
        every time a *different* (live) contract entered its own pre-close
        window. The CPU saturation broke every websocket keepalive and
        starved the DB pool. The ticker-encoded close time is the authoritative
        signal here -- once it's in the past the position can never be
        managed via normal exit/settlement paths.
        """
        try:
            pool = self._pool
            if pool is None:
                return
            import json
            async with pool.connection() as conn:
                row = await conn.execute(
                    "SELECT value FROM bot_state WHERE key = 'paper_open_position'"
                )
                result = await row.fetchone()
            if not result:
                return
            data = result[0] if isinstance(result[0], dict) else json.loads(result[0])

            ticker = data.get("ticker", "")
            close_time = _ticker_close_time(ticker)
            now = datetime.now(timezone.utc)
            if close_time is not None and close_time <= now:
                age_h = (now - close_time).total_seconds() / 3600.0
                logger.warning(
                    "coordinator.paper_position_stale_dropped",
                    ticker=ticker,
                    contract_close_utc=close_time.isoformat(),
                    closed_hours_ago=round(age_h, 2),
                    direction=data.get("direction"),
                    contracts=data.get("contracts"),
                    entry_price=data.get("entry_price"),
                )
                await self._clear_paper_position()
                return

            try:
                entry_time = datetime.fromisoformat(data["entry_time"])
            except (KeyError, ValueError, TypeError):
                entry_time = datetime.now(timezone.utc)

            from execution.paper_trader import PaperPosition
            self.paper_trader.position = PaperPosition(
                ticker=data["ticker"],
                direction=data["direction"],
                contracts=int(data["contracts"]),
                entry_price=float(data["entry_price"]),
                entry_time=entry_time,
                conviction=data.get("conviction", "NORMAL"),
                regime_at_entry=data.get("regime_at_entry", "MEDIUM"),
                entry_obi=float(data.get("entry_obi") or 0.0),
                entry_roc=float(data.get("entry_roc") or 0.0),
                candles_held=int(data.get("candles_held") or 0),
                max_favorable_excursion=float(data.get("max_favorable_excursion") or 0.0),
                max_adverse_excursion=float(data.get("max_adverse_excursion") or 0.0),
                signal_driver=data.get("signal_driver", "-"),
                position_uid=str(data.get("position_uid") or ""),
            )
            logger.info("coordinator.paper_position_restored",
                        ticker=data["ticker"],
                        direction=data["direction"],
                        contracts=data["contracts"],
                        candles_held=data.get("candles_held", 0))
        except Exception as e:
            logger.warning("coordinator.restore_paper_position_failed", error=str(e))

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
                "exit_intel_promotion_sent": self._exit_intel_promotion_sent,
                "health_breach_counts": {
                    str(key): max(0, int(value))
                    for key, value in self._health_breach_counts.items()
                },
                "paper_exit_per_pair": [
                    {
                        "ticker": ticker,
                        "direction": direction,
                        "exit_ts": float(exit_ts),
                    }
                    for (ticker, direction), exit_ts in self._last_paper_exit_per_pair.items()
                    if ticker and direction
                ],
            }
            async with write_gate():
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
                    self._exit_intel_promotion_sent = state.get(
                        "exit_intel_promotion_sent", False
                    )
                    raw_health_counts = state.get("health_breach_counts", {})
                    if isinstance(raw_health_counts, dict):
                        restored_health_counts: dict[str, int] = {}
                        for key, value in raw_health_counts.items():
                            try:
                                restored_health_counts[str(key)] = max(0, int(value))
                            except (TypeError, ValueError):
                                continue
                        self._health_breach_counts = restored_health_counts
                    else:
                        self._health_breach_counts = {}
                    raw_pair_exits = state.get("paper_exit_per_pair", [])
                    restored_pair_exits: dict[tuple[str, str], float] = {}
                    if isinstance(raw_pair_exits, list):
                        for row in raw_pair_exits:
                            if not isinstance(row, dict):
                                continue
                            ticker = str(row.get("ticker", "")).strip()
                            direction = str(row.get("direction", "")).strip().lower()
                            try:
                                exit_ts = float(row.get("exit_ts"))
                            except (TypeError, ValueError):
                                continue
                            if ticker and direction in {"long", "short"}:
                                restored_pair_exits[(ticker, direction)] = exit_ts
                    self._last_paper_exit_per_pair = restored_pair_exits
                    logger.info("coordinator.state_restored",
                                paper_bankroll=self.paper_sizer.bankroll,
                                live_bankroll=self.live_sizer.bankroll,
                                trading_mode=self.trading_mode)
                else:
                    self._apply_sizer_state(self.paper_sizer, state)
                    self._health_breach_counts = {}
                    self._last_paper_exit_per_pair = {}
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

    def get_exit_intelligence_state(self) -> dict[str, Any]:
        return {
            "enabled": settings.bot.exit_intelligence_enabled,
            "shadow_only": settings.bot.exit_intelligence_shadow_only,
            "threshold": settings.bot.health_score_threshold,
            "breach_ticks": settings.bot.health_score_breach_ticks,
            "position_telemetry_enabled": settings.bot.position_telemetry_enabled,
            "position_telemetry_interval_sec": (
                settings.bot.position_telemetry_interval_sec
            ),
            "promotion": {
                "alert_sent": self._exit_intel_promotion_sent,
                "min_paper_trades": (
                    settings.bot.exit_intel_promotion_min_paper_trades
                ),
                "min_distinct_regimes": (
                    settings.bot.exit_intel_promotion_min_distinct_regimes
                ),
                "min_distinct_hours": (
                    settings.bot.exit_intel_promotion_min_distinct_hours
                ),
            },
            "health": {
                "paper": self._last_health_snapshot.get("paper"),
                "live": self._last_health_snapshot.get("live"),
            },
        }

    async def _periodic_reconciliation_wrapper(self) -> None:
        """BUG-032 follow-up wrapper: clears the in-flight flag in
        ``finally`` so a failed reconcile doesn't permanently lock the
        next periodic call out."""
        try:
            await self._periodic_reconciliation()
        finally:
            self._periodic_reconcile_in_flight = False

    async def _periodic_reconciliation(self) -> None:
        """Periodic check: detect exchange positions the bot doesn't know about,
        and sync wallet balance to keep internal bankroll accurate.

        The wallet sync is throttled (force=False) so we don't hit Kalshi
        every ~2 seconds while the tick loop is hot. Event-driven callers
        (post-trade exit, reset-drawdown, toggle, ghost cleared) bypass
        the throttle.
        """
        try:
            await self._reconcile_live_positions()
        except Exception as e:
            logger.warning("coordinator.periodic_reconcile_failed", error=str(e))

        try:
            await self.sync_live_bankroll(force=False)
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
