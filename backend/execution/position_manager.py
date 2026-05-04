"""
PositionManager -- exchange-anchored position lifecycle with explicit state machine.

Single source of truth for live position state. All order operations (entry,
exit, orphan recovery, emergency stop) must go through this manager and acquire
its asyncio.Lock to prevent concurrent Kalshi API calls.

State machine:
    FLAT -> ENTERING -> OPEN -> EXITING -> FLAT
    ENTERING -> FLAT (rejected)
    EXITING -> PARTIAL_EXIT -> FLAT
    Any state -> DESYNC (reconciliation mismatch, blocks entries until resolved)
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import httpx
import structlog

from config import settings
from data.fill_stream import Fill, FillStream
from data.kalshi_ws import KalshiOrderClient

logger = structlog.get_logger(__name__)


class PositionState(str, Enum):
    FLAT = "FLAT"
    ENTERING = "ENTERING"
    OPEN = "OPEN"
    EXITING = "EXITING"
    PARTIAL_EXIT = "PARTIAL_EXIT"
    DESYNC = "DESYNC"


@dataclass
class ManagedPosition:
    ticker: str
    direction: str
    contracts: int
    entry_price: float
    entry_time: str
    conviction: str
    regime_at_entry: str
    entry_obi: float = 0.0
    entry_roc: float = 0.0
    candles_held: int = 0
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    order_id: Optional[str] = None
    entry_cost_dollars: Optional[float] = None
    entry_fees_dollars: Optional[float] = None
    signal_driver: str = "-"
    # BUG-025: which authority produced the entry price/cost/fees fields.
    # ``"fill_ws"`` means we drained per-execution Fill events from the
    # authenticated fill WebSocket. ``"order_response"`` is the historical
    # path that parses the polled REST order. Surfaced in trade rows so
    # operators can audit how often the WS engaged.
    entry_fill_source: str = "order_response"
    # BUG-025/BUG-027: snapshot of the live wallet balance (in dollars)
    # captured *before* the entry order was placed. Used by
    # ``coordinator._persist_trade`` to compute the round-trip wallet PnL
    # (``wallet_post_exit - wallet_at_entry``) and quarantine trades
    # whose recorded PnL drifts from the actual cash movement. Capturing
    # pre-entry is critical: a post-entry snapshot would already have
    # the entry debit baked in and the diff would only reflect the exit
    # leg's cash flow, making the drift metric structurally wrong.
    wallet_at_entry: Optional[float] = None


@dataclass
class OrphanedPosition:
    ticker: str
    direction: str
    contracts: int
    avg_entry_price: float
    detected_at: str
    # Optional upstream cause for analytics. Set to "EXPIRY_409" when the
    # orphan was created because an exit hit a 409 Conflict at expiry; lets
    # check_orphans record the eventual settlement as EXPIRY_409_SETTLED
    # instead of the generic ORPHAN_SETTLED.
    cause: Optional[str] = None
    # Whether the supervised round-trip counter has already advanced for
    # this orphan. The 409 settlement path bumps eagerly, so check_orphans
    # must skip the bump in that case to avoid double-counting against the
    # ``live_trade_limit`` gate.
    counted: bool = False


# Kalshi canonical terminal statuses (API v2 spec 3.13.0)
TERMINAL_STATUSES = ("executed", "canceled")

FILL_POLL_INTERVAL = 0.25
FILL_POLL_TIMEOUT = 15.0
VERIFY_FAILED = -1

# BUG-022: When an entry "market" order rests on the book (Kalshi treats
# the price field as a limit floor), short-circuit the poll loop after
# this many seconds so the caller can cancel the order and re-verify.
# Without this we wait the full FILL_POLL_TIMEOUT, widening the window in
# which the resting order can match and become an orphan.
ENTRY_REST_BAILOUT_SEC = 2.0

# BUG-022: Settle time after canceling a resting entry order before
# we re-query the positions endpoint. Mirrors the BUG-002 ledger-lag
# pattern used after entry/exit fills.
ENTRY_CANCEL_SETTLE_SEC = 1.0

# BUG-025: How long the fill-stream drain may block waiting for per-
# execution Fill events. Strictly bounded so a missed WS frame can never
# starve the entry/exit state machine. The drain runs *after* the order
# has already terminalized so it only adds wait time when fills arrive
# after the REST poll returns -- typically zero-cost on the happy path.
FILL_STREAM_DRAIN_TIMEOUT_SEC = 2.0


class PositionManager:
    """Thread-safe, exchange-anchored position lifecycle manager.

    All mutations (enter, exit, reconcile, emergency close) acquire self._lock
    so only one order operation can be in-flight at any time.
    """

    def __init__(
        self,
        client: KalshiOrderClient,
        fill_stream: Optional[FillStream] = None,
        *,
        live_trade_limit: Optional[int] = None,
    ):
        self.client = client
        # BUG-025: optional authoritative source for per-execution fill
        # data. When unset (tests, paper-only, dev) the manager keeps
        # using the legacy ``_parse_fill_*`` parsers verbatim.
        self.fill_stream = fill_stream
        self._lock = asyncio.Lock()
        self.state = PositionState.FLAT
        self.position: Optional[ManagedPosition] = None
        self.orphaned_positions: list[OrphanedPosition] = []
        self._db_pool = None

        # Supervised live-trade cap. After this many completed live round-
        # trips, ``can_enter`` returns False until ``reset_trade_counter`` is
        # called. None = unlimited. Sourced from ``BotConfig.live_trade_limit``
        # by the production callsite (LiveTrader); tests pass it explicitly.
        # The COUNTER (``_completed_live_trades``) is restored from snapshot;
        # the LIMIT is always taken from the constructor / env so changing
        # ``LIVE_TRADE_LIMIT`` is a simple env edit + restart, no DB wipe.
        self.live_trade_limit: Optional[int] = live_trade_limit
        self._completed_live_trades: int = 0

        # Tracks tickers confirmed as settled/finalized to prevent
        # reconciliation from re-adopting them as orphans (BUG-008).
        self._settled_tickers: set[str] = set()

        # Per-ticker cooldown: skip reconciliation for recently-exited tickers
        # to avoid race conditions with Kalshi's position API lag.
        self._exit_cooldowns: dict[str, float] = {}
        self.RECONCILE_COOLDOWN_SEC = 90.0

        # Per-ticker entry cooldown: after a phantom_entry_prevented event,
        # immediately re-attempting the same ticker reproduces the same race
        # because the in-flight resting order may still be bouncing through
        # Kalshi's books. A short cooldown (default 30s) lets the exchange
        # state stabilise. Configurable per instance for tests.
        self._entry_phantom_cooldowns: dict[str, float] = {}
        self.PHANTOM_ENTRY_COOLDOWN_SEC = 30.0

        # Phase 3 (Expiry Exit Reliability, 2026-05-04): pre-expiry passive
        # limit ladder telemetry. Counters are process-local; survive
        # neither restart nor a manual state wipe by design (telemetry,
        # not authoritative state).
        self._ladder_runs: int = 0
        self._ladder_full_fills: int = 0
        self._ladder_partial_fills: int = 0
        self._ladder_no_fills: int = 0
        self._ladder_fallbacks: int = 0
        self._ladder_in_flight_ticker: Optional[str] = None

        # Phase 4 (Expiry Exit Reliability, 2026-05-04): TELEMETRY-ONLY
        # counters for the deferred orphan-cadence and entry-depth-gating
        # workstream. These do NOT change execution behavior. Their job
        # is to inform the activation rubric in
        # docs/runbooks/live-edge-filters.md so we can decide later
        # whether to flip the gate. Each counter is accompanied by an
        # ``observed`` total so the ratio (blocked / observed) is
        # interpretable.
        #
        # ``orphan_break_even_observed``: every iteration where we have
        #   an orphan and a fresh market quote.
        # ``orphan_break_even_blocked``: subset where the bid was below
        #   the orphan's avg_entry_price (recovery would have crystalized
        #   a loss). The deferred-tolerance proposal would let us widen
        #   this gate if blocked / observed exceeds the documented
        #   threshold over the documented window.
        self._orphan_break_even_observed: int = 0
        self._orphan_break_even_blocked: int = 0
        # Hypothetical entry-depth rejects near expiry. Computed at
        # decision time via ``record_entry_depth_observation`` from the
        # coordinator; never enforced. Pure observation hook.
        self._near_expiry_depth_observed: int = 0
        self._near_expiry_depth_would_block: int = 0

    # ── Public properties ─────────────────────────────────────────────

    @property
    def has_position(self) -> bool:
        return self.position is not None

    @property
    def has_orphans(self) -> bool:
        return len(self.orphaned_positions) > 0

    @property
    def is_busy(self) -> bool:
        return self._lock.locked()

    @property
    def can_enter(self) -> bool:
        if self.live_trade_limit is not None and self._completed_live_trades >= self.live_trade_limit:
            return False
        return (
            self.state == PositionState.FLAT
            and not self.has_orphans
            and not self.is_busy
        )

    def set_db_pool(self, pool) -> None:
        self._db_pool = pool

    def _record_exit_cooldown(self, ticker: str) -> None:
        """Mark a ticker as recently exited so reconciliation skips it."""
        self._exit_cooldowns[ticker] = time.time()

    def _is_in_cooldown(self, ticker: str) -> bool:
        """True if ticker exited too recently for reliable reconciliation."""
        exit_time = self._exit_cooldowns.get(ticker)
        if exit_time is None:
            return False
        elapsed = time.time() - exit_time
        if elapsed > self.RECONCILE_COOLDOWN_SEC:
            del self._exit_cooldowns[ticker]
            return False
        return True

    def _record_phantom_entry_cooldown(self, ticker: str) -> None:
        """Mark a ticker as just-prevented so quick re-entries are blocked."""
        self._entry_phantom_cooldowns[ticker] = time.time()

    def _is_in_phantom_entry_cooldown(self, ticker: str) -> bool:
        """True if ticker is still inside its post-phantom cooldown window."""
        recorded = self._entry_phantom_cooldowns.get(ticker)
        if recorded is None:
            return False
        elapsed = time.time() - recorded
        if elapsed > self.PHANTOM_ENTRY_COOLDOWN_SEC:
            del self._entry_phantom_cooldowns[ticker]
            return False
        return True

    def can_enter_ticker(self, ticker: str) -> bool:
        """Public pre-check used by the coordinator to avoid spinning up an
        entry task when the position manager would refuse anyway. Mirrors the
        gating performed inside ``enter()``; lock-free so it's safe from the
        synchronous coordinator path.
        """
        if not self.can_enter:
            return False
        if self._is_in_phantom_entry_cooldown(ticker):
            return False
        return True

    # ── State transitions ─────────────────────────────────────────────

    def _transition(self, new_state: PositionState) -> None:
        old = self.state
        self.state = new_state
        logger.info("position_manager.state_transition",
                     old=old.value, new=new_state.value,
                     ticker=self.position.ticker if self.position else None)
        asyncio.ensure_future(self._persist_state())

    # ── Client order ID generation ────────────────────────────────────

    @staticmethod
    def _generate_client_order_id(ticker: str, action: str) -> str:
        ts_ms = int(time.time() * 1000)
        short_id = uuid.uuid4().hex[:8]
        return f"{ticker}-{action}-{ts_ms}-{short_id}"

    # ── Order fill polling (Kalshi API v2) ────────────────────────────

    @staticmethod
    def _parse_fill_count(order_data: dict) -> int:
        fp = order_data.get("fill_count_fp")
        if fp is not None:
            try:
                return int(float(fp))
            except (ValueError, TypeError):
                pass
        return 0

    @staticmethod
    def _parse_remaining_count(order_data: dict) -> int:
        fp = order_data.get("remaining_count_fp")
        if fp is not None:
            try:
                return int(float(fp))
            except (ValueError, TypeError):
                pass
        return -1

    @staticmethod
    def _parse_fill_price(order_data: dict, direction: str) -> Optional[float]:
        if direction == "long":
            raw = order_data.get("yes_price_dollars") or order_data.get("yes_price")
        else:
            raw = order_data.get("no_price_dollars") or order_data.get("no_price")
        if raw is not None:
            try:
                val = float(raw)
                return val * 100 if val < 1 else val
            except (ValueError, TypeError):
                pass
        return None

    @staticmethod
    def _parse_fill_price_yes_side(order_data: dict) -> Optional[float]:
        """Always return the Yes-side price in cents from a fill.

        The PnL formula uses Yes-side prices uniformly:
          long  PnL = +(exit_yes - entry_yes)  (buy Yes low, sell Yes high)
          short PnL = -(exit_yes - entry_yes)  (sell Yes high, buy Yes low)

        Kalshi order responses include both yes_price_dollars and
        no_price_dollars. We prefer yes_price_dollars directly; if only
        no_price_dollars is available we convert via (1 - no_price).
        """
        yes_raw = order_data.get("yes_price_dollars") or order_data.get("yes_price")
        if yes_raw is not None:
            try:
                val = float(yes_raw)
                return val * 100 if val < 1 else val
            except (ValueError, TypeError):
                pass
        no_raw = order_data.get("no_price_dollars") or order_data.get("no_price")
        if no_raw is not None:
            try:
                no_val = float(no_raw)
                if no_val < 1:
                    no_val *= 100
                return 100 - no_val
            except (ValueError, TypeError):
                pass
        return None

    @staticmethod
    def _parse_actual_fees(order_data: dict) -> Optional[float]:
        """Extract actual fees paid from Kalshi order response."""
        taker = order_data.get("taker_fees_dollars")
        maker = order_data.get("maker_fees_dollars")
        if taker is not None or maker is not None:
            try:
                return float(taker or "0") + float(maker or "0")
            except (ValueError, TypeError):
                pass
        return None

    @staticmethod
    def _parse_fill_cost(order_data: dict) -> Optional[float]:
        """Extract taker_fill_cost_dollars -- the actual dollar amount Kalshi
        charged/credited for this order, independent of Yes/No side."""
        raw = order_data.get("taker_fill_cost_dollars")
        if raw is not None:
            try:
                return float(raw)
            except (ValueError, TypeError):
                pass
        return None

    # ── BUG-025: fill-stream drain helper ─────────────────────────────

    async def _drain_fill_stream(
        self,
        order_id: Optional[str],
        *,
        min_count: int,
        leg: str,
    ) -> tuple[list[Fill], str]:
        """Drain per-execution Fill events for ``order_id`` from the WS.

        Returns ``(fills, source)`` where ``source`` is:
          * ``"fill_ws"`` -- at least one Fill was returned and matched
            ``min_count``. Caller should override price/cost/fees from
            the aggregated VWAP.
          * ``"fill_ws_partial"`` -- some Fills were returned but the
            cumulative count is below ``min_count``. Caller should still
            prefer the WS values (they're truth for what filled), but
            log so we can tell apart from a clean drain.
          * ``"order_response"`` -- no Fills returned (stream off, miss,
            timeout). Caller falls back to the existing
            ``_parse_fill_*`` parsers; **no behavior change vs. today**.

        ``leg`` is just a log label (``"entry"`` / ``"exit"`` / ``"orphan"``).
        """
        if self.fill_stream is None or not order_id or min_count <= 0:
            return [], "order_response"
        try:
            fills = await self.fill_stream.drain_for_order(
                order_id,
                min_count=min_count,
                timeout_sec=FILL_STREAM_DRAIN_TIMEOUT_SEC,
            )
        except Exception as e:
            logger.warning(
                "position_manager.fill_stream_drain_error",
                leg=leg, order_id=order_id, error=str(e),
            )
            return [], "order_response"
        if not fills:
            logger.info(
                "position_manager.fill_stream_miss",
                leg=leg, order_id=order_id, min_count=min_count,
            )
            return [], "order_response"
        total = sum(f.count for f in fills)
        source = "fill_ws" if total >= min_count else "fill_ws_partial"
        vwap = FillStream.vwap_yes_cents(fills)
        cost = FillStream.total_cost_dollars(fills)
        fees = FillStream.total_fees_dollars(fills)
        logger.info(
            "position_manager.fill_stream_capture",
            leg=leg, order_id=order_id,
            executions=len(fills),
            count=total, expected=min_count,
            vwap_yes_cents=round(vwap, 4) if vwap is not None else None,
            cost_dollars=round(cost, 4),
            fees_dollars=round(fees, 4),
            source=source,
        )
        return fills, source

    async def _poll_order_fill(
        self,
        order_id: str,
        *,
        early_rest_bailout_sec: Optional[float] = None,
        timeout_sec: Optional[float] = None,
    ) -> dict:
        """Poll Kalshi for order terminalization.

        Returns the order_data dict from the most recent successful poll.

        Args:
            order_id: Kalshi order id returned by create_order.
            early_rest_bailout_sec: If set and the order is observed to be
                ``status == "resting"`` after this many seconds elapsed,
                short-circuit the loop and return the resting order_data
                so the caller can cancel it. Defaults to None (no early
                bailout, original behavior).
            timeout_sec: Override for FILL_POLL_TIMEOUT. Used by
                EXPIRY_GUARD/SHORT_SETTLEMENT_GUARD exits, where the
                contract is about to close and we need to fail fast
                rather than sit on a 15s poll loop. None = use module
                default (BUG-032).
        """
        timeout = timeout_sec if timeout_sec is not None else FILL_POLL_TIMEOUT
        elapsed = 0.0
        last_order_data: dict = {}
        while elapsed < timeout:
            await asyncio.sleep(FILL_POLL_INTERVAL)
            elapsed += FILL_POLL_INTERVAL
            try:
                detail = await self.client.get_order(order_id)
                order_data = detail.get("order", {})
                last_order_data = order_data
                status = order_data.get("status", "")
                if status in TERMINAL_STATUSES:
                    return order_data
                if (
                    early_rest_bailout_sec is not None
                    and status == "resting"
                    and elapsed >= early_rest_bailout_sec
                ):
                    logger.info("position_manager.poll_order_early_bailout",
                                order_id=order_id, status=status,
                                elapsed=elapsed,
                                bailout_sec=early_rest_bailout_sec)
                    return order_data
            except Exception as e:
                logger.warning("position_manager.poll_order_error",
                               order_id=order_id, error=str(e), elapsed=elapsed)
        logger.warning("position_manager.poll_order_timeout",
                        order_id=order_id, timeout=timeout)
        try:
            detail = await self.client.get_order(order_id)
            return detail.get("order", {})
        except Exception:
            return last_order_data

    async def _cancel_entry_order_safely(
        self, order_id: str, ticker: str
    ) -> bool:
        """Cancel a (presumed-resting) entry order, swallowing the 404
        Kalshi returns when the order has already terminalized.

        Returns True if cancel API succeeded (order is now canceled or was
        already terminal); False on a non-404 error.

        BUG-022: Without this, an entry "market" order that rested on the
        book can match minutes later and create an orphan position. Calling
        this immediately after a non-terminal poll closes that window.
        """
        try:
            await self.client.cancel_order(order_id)
            logger.info("position_manager.entry_canceled_on_timeout",
                        ticker=ticker, order_id=order_id)
            return True
        except httpx.HTTPStatusError as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                logger.info("position_manager.entry_cancel_already_terminal",
                            ticker=ticker, order_id=order_id, http_status=status)
                return True
            logger.warning("position_manager.entry_cancel_failed",
                           ticker=ticker, order_id=order_id,
                           http_status=status, error=str(e))
            return False
        except Exception as e:
            logger.warning("position_manager.entry_cancel_failed",
                           ticker=ticker, order_id=order_id, error=str(e))
            return False

    # ── Exchange verification ─────────────────────────────────────────

    async def verify_position_on_exchange(self, ticker: str) -> int:
        """Query Kalshi for actual signed contract count on this ticker.

        Positive = long, negative = short, 0 = flat.
        Returns VERIFY_FAILED (-1) on API error.
        """
        try:
            data = await self.client.get_positions(
                ticker=ticker, count_filter="position"
            )
            for mp in data.get("market_positions", []):
                if mp.get("ticker") == ticker:
                    return int(float(mp.get("position_fp", 0)))
            return 0
        except Exception as e:
            logger.error("position_manager.verify_failed",
                         ticker=ticker, error=str(e))
            return VERIFY_FAILED

    async def _check_if_market_settled(self, ticker: str) -> Optional[str]:
        """Check if a market has settled. Returns 'yes'/'no' if settled, None if still open."""
        try:
            market_data = await self.client.get_market(ticker)
            market = market_data.get("market", {})
            status = market.get("status", "")
            if status in ("closed", "settled", "finalized"):
                result = market.get("result", "")
                self._settled_tickers.add(ticker)
                return result or "no"
        except Exception as e:
            logger.warning("position_manager.market_settled_check_failed",
                           ticker=ticker, error=str(e))
        return None

    async def _verify_with_retry(self, ticker: str, retries: int = 3,
                                  backoff: float = 2.0) -> int:
        """verify_position_on_exchange with retries on VERIFY_FAILED.

        During settlement the Kalshi API often returns transient errors.
        Retrying avoids false-negative orphan adoption.
        """
        for attempt in range(1, retries + 1):
            result = await self.verify_position_on_exchange(ticker)
            if result != VERIFY_FAILED:
                return result
            if attempt < retries:
                wait = backoff * attempt
                logger.warning("position_manager.verify_retry",
                               ticker=ticker, attempt=attempt, wait=wait)
                await asyncio.sleep(wait)
        logger.error("position_manager.verify_exhausted",
                     ticker=ticker, retries=retries)
        return VERIFY_FAILED

    async def _check_flat_on_exchange(self, ticker: str) -> bool:
        """Return True only if exchange confirms zero position on ticker."""
        count = await self.verify_position_on_exchange(ticker)
        if count == VERIFY_FAILED:
            logger.warning("position_manager.pre_entry_verify_failed", ticker=ticker)
            return False
        if count != 0:
            logger.warning("position_manager.not_flat_on_exchange",
                           ticker=ticker, exchange_contracts=count)
            return False
        return True

    # Terminal statuses where Kalshi will not accept new orders.
    # Anything NOT in this set is treated as tradeable.
    #
    # BUG-030: the original code used an allowlist (``status != "open"``)
    # but Kalshi's API returns ``"active"`` for most tradeable KXBTC
    # contracts, which silently blocked every live entry since 2026-04-27.
    # Using a blocklist is safer: if Kalshi adds a new tradeable status
    # tomorrow, we'll accept it by default instead of silently halting.
    _TERMINAL_STATUSES = frozenset({
        "closed", "closing", "settled", "finalized", "determined", "halted",
    })

    async def _market_open_for_entry(self, ticker: str) -> bool:
        """BUG-028/BUG-030: confirm Kalshi reports the market as tradeable
        right before placing an entry order.

        Returns True if the market is NOT in a terminal status. Returns
        True (fail-open) if the recheck call itself errors — the
        coordinator's time-to-expiry guard is the primary defense and we
        don't want a transient REST blip to block a valid signal.
        """
        try:
            data = await self.client.get_market(ticker)
            market = data.get("market", data)
            status = (market.get("status") or "").lower()
            if status in self._TERMINAL_STATUSES:
                logger.info(
                    "position_manager.market_not_open_pre_entry",
                    ticker=ticker, status=status,
                )
                return False
            return True
        except Exception as e:
            logger.warning(
                "position_manager.market_status_recheck_failed",
                ticker=ticker, error=str(e),
            )
            return True

    async def _recover_order_after_failure(self, client_order_id: str) -> Optional[dict]:
        """After create_order throws, check if the order actually went through."""
        try:
            orders_data = await self.client.get_orders(status="resting")
            for order in orders_data.get("orders", []):
                if order.get("client_order_id") == client_order_id:
                    return order
            orders_data = await self.client.get_orders(status="executed")
            for order in orders_data.get("orders", []):
                if order.get("client_order_id") == client_order_id:
                    return order
        except Exception as e:
            logger.error("position_manager.recover_order_failed",
                         client_order_id=client_order_id, error=str(e))
        return None

    # ── ENTER ─────────────────────────────────────────────────────────

    async def enter(
        self,
        ticker: str,
        direction: str,
        contracts: int,
        price: float,
        conviction: str,
        regime: str,
        obi: float = 0.0,
        roc: float = 0.0,
        signal_driver: str = "-",
    ) -> Optional[ManagedPosition]:
        """Place an entry order with full exchange verification.

        Acquires the position lock. Only one order operation at a time.
        """
        async with self._lock:
            if self.live_trade_limit is not None and self._completed_live_trades >= self.live_trade_limit:
                logger.warning("position_manager.enter_blocked_trade_limit",
                               completed=self._completed_live_trades,
                               limit=self.live_trade_limit)
                return None

            if self.state != PositionState.FLAT:
                logger.warning("position_manager.enter_rejected_state",
                               state=self.state.value)
                return None

            if self.has_orphans:
                logger.warning("position_manager.enter_blocked_orphans",
                               count=len(self.orphaned_positions))
                return None

            # BUG-022 follow-up: cool down the ticker that just produced a
            # phantom_entry_prevented event. Quick re-entries reproduce the
            # same race because in-flight resting orders may still be
            # bouncing through Kalshi's books.
            if self._is_in_phantom_entry_cooldown(ticker):
                cooldown_remaining = round(
                    self.PHANTOM_ENTRY_COOLDOWN_SEC
                    - (time.time() - self._entry_phantom_cooldowns[ticker]),
                    2,
                )
                logger.info(
                    "position_manager.enter_blocked_phantom_cooldown",
                    ticker=ticker,
                    cooldown_remaining_sec=max(cooldown_remaining, 0.0),
                )
                return None

            # Pre-order: confirm exchange is flat
            if not await self._check_flat_on_exchange(ticker):
                self._transition(PositionState.DESYNC)
                return None

            # BUG-028 layer-2: refuse to place an entry order against a
            # market that Kalshi reports as anything other than ``open``.
            # The coordinator's pre-evaluation expiry guard is the primary
            # defense, but this is a cheap (~25ms) backstop against the
            # ~10-100ms race between "decide to enter" and
            # "create_order hits the wire". When the time guard above let
            # us pass because ``state.expiry_time`` was None (e.g. the
            # ticker just rotated and no ``ticker`` WS event has populated
            # it yet) this REST recheck is the only thing standing
            # between us and an EXPIRY_409_SETTLED trade.
            #
            # Fail-open: if the recheck call itself errors (Kalshi 5xx,
            # network blip), we log and proceed. Treating a transient REST
            # failure as ``status != open`` would convert one class of
            # rare race into a different class of avoidable miss; the
            # primary time guard already caught the common case.
            if (settings.bot.expiry_market_status_check_enabled
                    and not await self._market_open_for_entry(ticker)):
                logger.warning(
                    "position_manager.enter_blocked_market_not_open",
                    ticker=ticker,
                )
                self._transition(PositionState.FLAT)
                return None

            # BUG-025/BUG-027: capture the wallet balance *before* placing
            # the entry order. ``coordinator._persist_trade`` diffs this
            # against the post-exit wallet to compute the round-trip cash
            # PnL (``wallet_post - wallet_pre``). The previous version
            # captured *after* the entry debit had already cleared, so the
            # diff only saw the exit leg's cash flow and the drift metric
            # always disagreed with the (also-broken) recorded PnL by
            # roughly the entry cost. Capturing before order placement
            # makes ``wallet_pnl`` an authoritative round-trip figure.
            wallet_at_entry: Optional[float] = None
            try:
                bal_data = await self.client.get_balance()
                wallet_at_entry = float(bal_data.get("balance", 0)) / 100.0
            except Exception as e:
                logger.warning(
                    "position_manager.wallet_capture_failed",
                    leg="entry_pre", ticker=ticker, error=str(e),
                )

            self._transition(PositionState.ENTERING)

            side = "yes" if direction == "long" else "no"
            yes_price = int(price) if direction == "long" else None
            no_price = int(100 - price) if direction == "short" else None
            client_order_id = self._generate_client_order_id(ticker, "buy")

            order_id = None
            try:
                result = await self.client.create_order(
                    ticker=ticker,
                    side=side,
                    action="buy",
                    count=contracts,
                    type="market",
                    yes_price=yes_price,
                    no_price=no_price,
                    client_order_id=client_order_id,
                )
                order_id = result.get("order", {}).get("order_id")
                logger.info("position_manager.order_placed",
                            ticker=ticker, direction=direction,
                            contracts=contracts, order_id=order_id,
                            client_order_id=client_order_id)
            except Exception as e:
                logger.error("position_manager.order_failed",
                             error=str(e), ticker=ticker,
                             client_order_id=client_order_id)
                # Check if order went through despite the exception
                recovered = await self._recover_order_after_failure(client_order_id)
                if recovered:
                    order_id = recovered.get("order_id")
                    logger.warning("position_manager.order_recovered_after_error",
                                   order_id=order_id)
                else:
                    exchange_count = await self.verify_position_on_exchange(ticker)
                    if exchange_count != 0 and exchange_count != VERIFY_FAILED:
                        logger.error("position_manager.silent_fill_detected",
                                     ticker=ticker, exchange_contracts=exchange_count)
                        self._transition(PositionState.DESYNC)
                        return None
                    self._transition(PositionState.FLAT)
                    return None

            # Poll for fill. BUG-022: bail out early if the order is
            # observed resting (Kalshi treated yes_price/no_price as a
            # limit floor) so we can cancel before it matches later.
            fill_price = price
            filled_contracts = 0
            entry_cost = None
            entry_fees = None
            order_data: dict = {}
            status = ""
            poll_filled = 0
            poll_canceled_resting = False
            if order_id:
                order_data = await self._poll_order_fill(
                    order_id, early_rest_bailout_sec=ENTRY_REST_BAILOUT_SEC,
                )
                status = order_data.get("status", "")
                poll_filled = self._parse_fill_count(order_data)

                if status == "canceled" and poll_filled == 0:
                    logger.warning("position_manager.entry_canceled",
                                   ticker=ticker, order_id=order_id)
                    self._transition(PositionState.FLAT)
                    return None

                # BUG-022: any non-terminal status (resting, executing,
                # unknown) means a portion of the order may still match
                # later. Cancel now, then re-poll to capture any partial
                # fill that may have occurred during cancel.
                if status not in TERMINAL_STATUSES:
                    poll_canceled_resting = True
                    await self._cancel_entry_order_safely(order_id, ticker)
                    await asyncio.sleep(ENTRY_CANCEL_SETTLE_SEC)
                    try:
                        detail = await self.client.get_order(order_id)
                        order_data = detail.get("order", {}) or order_data
                        status = order_data.get("status", "") or status
                        poll_filled = self._parse_fill_count(order_data)
                    except Exception as e:
                        logger.warning(
                            "position_manager.post_cancel_poll_failed",
                            ticker=ticker, order_id=order_id, error=str(e))

                if poll_filled > 0:
                    filled_contracts = poll_filled

                parsed_price = self._parse_fill_price_yes_side(order_data)
                if parsed_price is not None:
                    fill_price = parsed_price

                entry_cost = self._parse_fill_cost(order_data)
                entry_fees = self._parse_actual_fees(order_data)

            # Ledger lag: wait before verification so Kalshi's position
            # endpoint reflects the fill that just occurred (Fix 2). Skip
            # the extra sleep if we already slept after canceling.
            if not poll_canceled_resting:
                await asyncio.sleep(1.5)

            # BUG-022: use retrying verify so a transient stale-positions
            # read doesn't push us into phantom_entry_prevented when a fill
            # is still propagating through Kalshi's ledger.
            verified = await self._verify_with_retry(
                ticker, retries=3, backoff=1.5,
            )
            if verified == VERIFY_FAILED:
                if filled_contracts > 0:
                    logger.warning("position_manager.entry_verify_failed_trusting_poll",
                                   ticker=ticker, filled=filled_contracts)
                else:
                    logger.error("position_manager.entry_unverifiable",
                                 ticker=ticker, order_id=order_id)
                    self._transition(PositionState.DESYNC)
                    return None
            elif verified == 0:
                if filled_contracts > 0:
                    logger.error("position_manager.entry_poll_disagrees_exchange",
                                 ticker=ticker, poll_filled=filled_contracts,
                                 exchange=0)
                logger.info("position_manager.phantom_entry_prevented",
                            ticker=ticker, status=status,
                            canceled_resting=poll_canceled_resting,
                            order_id=order_id)
                self._transition(PositionState.FLAT)
                # Block re-entries on the same ticker for the cooldown
                # window so the coordinator doesn't burn through retries
                # while the cancelled order is still settling on Kalshi.
                self._record_phantom_entry_cooldown(ticker)
                return None
            else:
                # Exchange is the source of truth — even if poll reported a
                # different (e.g. zero) fill count, trust the verified count.
                if poll_canceled_resting and filled_contracts == 0:
                    logger.warning(
                        "position_manager.entry_filled_after_cancel",
                        ticker=ticker, order_id=order_id,
                        verified=verified)
                    # Final order fetch to recover fill price/cost/fees that
                    # the resting poll missed.
                    try:
                        detail = await self.client.get_order(order_id)
                        order_data = detail.get("order", {}) or order_data
                        parsed_price = self._parse_fill_price_yes_side(order_data)
                        if parsed_price is not None:
                            fill_price = parsed_price
                        recovered_cost = self._parse_fill_cost(order_data)
                        if recovered_cost is not None:
                            entry_cost = recovered_cost
                        recovered_fees = self._parse_actual_fees(order_data)
                        if recovered_fees is not None:
                            entry_fees = recovered_fees
                    except Exception as e:
                        logger.warning(
                            "position_manager.fill_recovery_fetch_failed",
                            ticker=ticker, order_id=order_id, error=str(e))
                filled_contracts = verified

            # BUG-025: prefer per-execution Fill events from the
            # authenticated WS over the polled order response. Order
            # responses can return stale ``yes_price_dollars`` / under-
            # report ``taker_fill_cost_dollars``; the fill stream is the
            # only source that matches the actual cash movement. Falls
            # back transparently to the parsed values when the stream is
            # disconnected, missing fills, or returned partial data.
            ws_fills, fill_source = await self._drain_fill_stream(
                order_id, min_count=filled_contracts, leg="entry",
            )
            if ws_fills:
                ws_vwap = FillStream.vwap_yes_cents(ws_fills)
                if ws_vwap is not None:
                    fill_price = ws_vwap
                entry_cost = FillStream.total_cost_dollars(ws_fills)
                entry_fees = FillStream.total_fees_dollars(ws_fills)

            self.position = ManagedPosition(
                ticker=ticker,
                direction=direction,
                contracts=filled_contracts,
                entry_price=fill_price,
                entry_time=datetime.now(timezone.utc).isoformat(),
                conviction=conviction,
                regime_at_entry=regime,
                entry_obi=obi,
                entry_roc=roc,
                order_id=order_id,
                entry_cost_dollars=entry_cost,
                entry_fees_dollars=entry_fees,
                signal_driver=signal_driver,
                entry_fill_source=fill_source,
                wallet_at_entry=wallet_at_entry,
            )
            self._transition(PositionState.OPEN)

            logger.info("position_manager.entry_confirmed",
                         ticker=ticker, direction=direction,
                         contracts=filled_contracts, price=fill_price,
                         entry_fill_source=fill_source,
                         wallet_at_entry=wallet_at_entry)
            return self.position

    # ── EXIT ──────────────────────────────────────────────────────────

    async def exit(
        self,
        price: float,
        reason: str,
        attempt: int = 0,
    ) -> Optional[dict]:
        """Place an exit order with full exchange verification.

        Returns a dict with trade details on success, None on failure.
        If a 409 Conflict reveals the market settled, delegates to
        handle_settlement automatically.
        Acquires the position lock.

        Phase 2 (Expiry Exit Reliability, 2026-05-04): ``attempt`` is
        the 0-based retry index supplied by the coordinator's retry
        loop. Used by ``_compute_expiry_retry_floor`` to widen the
        order-side floor on EXPIRY_GUARD / SHORT_SETTLEMENT_GUARD
        retries. Defaults to 0 so all non-coordinator callers (manual
        close, emergency_close, etc.) keep the legacy max-aggressive
        behavior.
        """
        async with self._lock:
            result = await self._exit_inner(price, reason, attempt=attempt)
            if result and result.get("_settled"):
                settled_result = result["_result"]
                via_409 = result.get("_via_409", False)
                logger.info("position_manager.exit_redirected_to_settlement",
                            result=settled_result, via_409=via_409)
                return await self._handle_settlement_inner(
                    settled_result, via_409=via_409,
                )
            return result

    # ── Phase 3: pre-expiry passive limit ladder ────────────────────────

    async def try_passive_limit_ladder(
        self,
        *,
        best_yes_bid: Optional[float],
        best_yes_ask: Optional[float],
        time_remaining_sec: Optional[float],
    ) -> Optional[dict]:
        """Attempt a pre-expiry passive limit ladder and return a fill dict
        on success, or None to fall through to the EXPIRY_GUARD path.

        Ladder mechanics (long exit, mirror for short):
          1. Place a passive limit sell at ``yes_price = best_yes_bid +
             rung_first_offset_cents`` (we ask for a higher price than
             the executable bid; the order rests on the ask side).
          2. Poll for fill up to ``ladder_rung_timeout_sec``.
          3. If filled fully, recover and return fill data (the trade
             completion handler will run via the standard exit flow).
          4. If partially filled, cancel the remainder and step to the
             next rung with the residual count. Aggregate fills across
             rungs.
          5. If no fill, cancel and step to the next (more aggressive)
             rung.
          6. Out of rungs OR out of time budget -> cancel any open
             rung and return None. The caller MUST then fall through
             to the EXPIRY_GUARD path for the residual.

        Safety:
          * Acquires self._lock for the whole ladder run; coordinator
            must check ``is_busy`` before calling.
          * The total budget is bounded by config; the ladder is
            guaranteed to release the lock before EXPIRY_GUARD fires.
          * If the time-remaining-sec drops below the ladder's
            absolute floor (start_trigger - total_budget) mid-ladder,
            we abort immediately so the guard path has its full window.

        Returns the same dict shape as ``_exit_inner`` on a complete
        fill so the caller can route through ``_handle_settlement_inner``
        if the contract resolved during the ladder. None means
        "ladder did not complete a full exit; please run EXPIRY_GUARD".
        """
        if self.position is None:
            return None
        cfg = settings.bot
        side = "yes" if self.position.direction == "long" else "no"

        # Front-door safety check. The ladder must finish within
        # start_trigger - guard_trigger seconds; if there's not enough
        # time left, refuse to start so we don't burn the guard window.
        if time_remaining_sec is None:
            return None
        absolute_floor_sec = (
            cfg.expiry_guard_trigger_sec + 5  # 5s margin for cancel/step
        )
        if time_remaining_sec <= absolute_floor_sec:
            return None
        if cfg.ladder_total_budget_sec >= (
            time_remaining_sec - absolute_floor_sec
        ):
            return None

        # Need at least one executable side to compute a rung price.
        if side == "yes" and best_yes_bid is None:
            return None
        if side == "no" and best_yes_ask is None:
            return None

        async with self._lock:
            # Re-check after acquiring the lock; another path may
            # have completed an exit while we were waiting.
            if self.position is None:
                return None
            self._ladder_runs += 1
            self._ladder_in_flight_ticker = self.position.ticker

            try:
                result = await self._run_ladder_rungs(
                    side=side,
                    best_yes_bid=best_yes_bid,
                    best_yes_ask=best_yes_ask,
                    cfg=cfg,
                )
            finally:
                self._ladder_in_flight_ticker = None

            if result is None:
                # No-fill outcome -- caller will fall back to EXPIRY_GUARD.
                self._ladder_no_fills += 1
                self._ladder_fallbacks += 1
                return None

            # Whole-position fill via the ladder. Use the same exit
            # bookkeeping as _exit_inner via _build_trade_result.
            self._ladder_full_fills += 1
            return result

    async def _run_ladder_rungs(
        self,
        *,
        side: str,
        best_yes_bid: Optional[float],
        best_yes_ask: Optional[float],
        cfg,
    ) -> Optional[dict]:
        """Inner ladder loop. Caller MUST hold self._lock."""
        if self.position is None:
            return None

        rung_count = max(1, cfg.ladder_rung_count)
        first_offset = max(1, cfg.ladder_rung_first_offset_cents)
        step = max(0, cfg.ladder_rung_step_cents)
        rung_timeout = max(1.0, float(cfg.ladder_rung_timeout_sec))
        total_budget = max(rung_timeout, float(cfg.ladder_total_budget_sec))

        ladder_started = time.monotonic()
        cumulative_fills: list[Fill] = []
        cumulative_filled = 0
        original_contracts = self.position.contracts
        rung_orders: list[str] = []

        for rung in range(rung_count):
            elapsed = time.monotonic() - ladder_started
            if elapsed >= total_budget:
                logger.info(
                    "position_manager.ladder_budget_exhausted",
                    ticker=self.position.ticker, rung=rung,
                    elapsed_sec=round(elapsed, 2),
                )
                break

            remaining_contracts = original_contracts - cumulative_filled
            if remaining_contracts <= 0:
                break

            # Compute rung price. For a long exit (sell YES) we set
            # yes_price = best_yes_bid + offset. The order rests on
            # the ask side at that price. Each rung WIDENS by ``step``
            # toward the bid, becoming more crossable.
            offset = max(1, first_offset - step * rung)
            if side == "yes":
                # Need the bid as the anchor.
                if best_yes_bid is None:
                    break
                rung_price = max(1, min(99, int(best_yes_bid) + offset))
            else:
                # Closing a short = sell NO. Use 100 - best_yes_ask
                # as the executable NO bid, then add offset.
                if best_yes_ask is None:
                    break
                no_anchor = max(0, 100 - int(best_yes_ask))
                rung_price = max(1, min(99, no_anchor + offset))

            client_order_id = self._generate_client_order_id(
                self.position.ticker, f"ladder-{rung}",
            )
            try:
                resp = await self.client.create_order(
                    ticker=self.position.ticker,
                    side=side,
                    action="sell",
                    count=remaining_contracts,
                    type="limit",
                    client_order_id=client_order_id,
                    **(
                        {"yes_price": rung_price}
                        if side == "yes" else {"no_price": rung_price}
                    ),
                )
                order_id = resp.get("order", {}).get("order_id")
                if order_id:
                    rung_orders.append(order_id)
                logger.info(
                    "position_manager.ladder_rung_placed",
                    ticker=self.position.ticker, rung=rung,
                    price=rung_price, side=side, order_id=order_id,
                    remaining_contracts=remaining_contracts,
                )
            except Exception as e:
                logger.warning(
                    "position_manager.ladder_rung_failed",
                    ticker=self.position.ticker, rung=rung,
                    error=str(e),
                )
                continue

            # Poll briefly for fills, then cancel and step.
            order_data = await self._poll_order_fill(
                order_id, timeout_sec=rung_timeout,
            )
            status = order_data.get("status", "")
            filled = self._parse_fill_count(order_data)
            if filled > 0:
                cumulative_filled += filled
                ws_fills, _src = await self._drain_fill_stream(
                    order_id, min_count=filled, leg="exit",
                )
                if ws_fills:
                    cumulative_fills.extend(ws_fills)

            if status not in ("executed", "canceled"):
                # Order is still resting -- cancel before stepping so
                # we don't leave duplicate sells on the book.
                try:
                    await self.client.cancel_order(order_id)
                except httpx.HTTPStatusError as e:
                    code = getattr(e.response, "status_code", None)
                    if code in (404, 410):
                        logger.info(
                            "position_manager.ladder_cancel_already_terminal",
                            ticker=self.position.ticker,
                            order_id=order_id, http_status=code,
                        )
                    else:
                        logger.warning(
                            "position_manager.ladder_cancel_failed",
                            ticker=self.position.ticker,
                            order_id=order_id, error=str(e),
                        )
                except Exception as e:
                    logger.warning(
                        "position_manager.ladder_cancel_failed",
                        ticker=self.position.ticker,
                        order_id=order_id, error=str(e),
                    )

            if cumulative_filled >= original_contracts:
                # Fully filled across rungs; break out and build trade.
                break

            if filled > 0 and filled < remaining_contracts:
                # Partial fill at this rung -- step to widen on residual.
                self._ladder_partial_fills += 1

        if cumulative_filled <= 0:
            return None

        # We have at least one fill. Compute the trade result. Use the
        # WS VWAP if available, otherwise the last poll's parsed price.
        if cumulative_fills:
            exit_price = FillStream.vwap_yes_cents(cumulative_fills)
            exit_cost = FillStream.total_cost_dollars(cumulative_fills)
            actual_fees = FillStream.total_fees_dollars(cumulative_fills)
            exit_fill_source = "fill_ws"
        else:
            exit_price = float(self.position.entry_price)
            exit_cost = None
            actual_fees = None
            exit_fill_source = "order_response"

        if cumulative_filled < original_contracts:
            # Partial-only ladder run -- abandon the residual and let
            # the caller (coordinator) run EXPIRY_GUARD on what's left.
            # We do NOT clear self.position; the residual lives on.
            self.position.contracts = original_contracts - cumulative_filled
            logger.info(
                "position_manager.ladder_partial_residual_to_guard",
                ticker=self.position.ticker,
                filled=cumulative_filled,
                residual=self.position.contracts,
            )
            return None

        # Full fill: build a trade result and clear position. We re-use
        # the same shape as _exit_inner's success path.
        return self._build_ladder_trade_result(
            exit_price=exit_price or 1.0,
            exit_cost_dollars=exit_cost,
            actual_fees=actual_fees,
            exit_fill_source=exit_fill_source,
            exited_contracts=cumulative_filled,
        )

    def _build_ladder_trade_result(
        self,
        *,
        exit_price: float,
        exit_cost_dollars: Optional[float],
        actual_fees: Optional[float],
        exit_fill_source: str,
        exited_contracts: int,
    ) -> dict:
        """Build the same dict shape as _exit_inner returns, then clear
        position. Caller must already hold the lock."""
        pos = self.position
        if pos is None:
            return {}
        # Compute cost-based PnL identical to _exit_inner.
        d = 1 if pos.direction == "long" else -1
        entry_cost = (
            pos.entry_cost_dollars
            if pos.entry_cost_dollars is not None
            else pos.entry_price * exited_contracts / 100.0
        )
        if exit_cost_dollars is None:
            exit_cost_dollars = exit_price * exited_contracts / 100.0
        if actual_fees is None:
            from risk.fee_engine import FeeEngine
            actual_fees = FeeEngine().compute_round_trip_fee(
                contracts=exited_contracts,
                entry_price_cents=pos.entry_price,
                exit_price_cents=exit_price,
                entry_type="taker",
                exit_type="maker",  # ladder fills as maker
            )
        gross = d * (exit_cost_dollars - entry_cost)
        net_pnl = gross - actual_fees
        notional = exited_contracts * pos.entry_price / 100.0
        pnl_pct = (net_pnl / notional) if notional > 0 else 0.0

        result = {
            "ticker": pos.ticker,
            "direction": pos.direction,
            "contracts": exited_contracts,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "pnl": round(net_pnl, 4),
            "pnl_pct": round(pnl_pct, 4),
            "fees": round(actual_fees, 4),
            "exit_reason": "EXPIRY_LADDER",
            "conviction": pos.conviction,
            "regime_at_entry": pos.regime_at_entry,
            "candles_held": pos.candles_held,
            "entry_time": pos.entry_time,
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "entry_obi": pos.entry_obi,
            "entry_roc": pos.entry_roc,
            "signal_driver": pos.signal_driver,
            "entry_cost_dollars": entry_cost,
            "exit_cost_dollars": exit_cost_dollars,
            "entry_fill_source": pos.entry_fill_source,
            "exit_fill_source": exit_fill_source,
            "wallet_at_entry": getattr(pos, "wallet_at_entry", None),
            "max_favorable_excursion": pos.max_favorable_excursion,
            "max_adverse_excursion": pos.max_adverse_excursion,
        }
        # Clear position on a complete exit.
        self.position = None
        self._transition(PositionState.FLAT)
        self._completed_live_trades += 1
        self._exit_cooldowns[result["ticker"]] = time.monotonic()
        return result

    def get_ladder_telemetry(self) -> dict:
        """Read-only telemetry snapshot for /api/diagnostics."""
        return {
            "runs": self._ladder_runs,
            "full_fills": self._ladder_full_fills,
            "partial_fills": self._ladder_partial_fills,
            "no_fills": self._ladder_no_fills,
            "fallbacks": self._ladder_fallbacks,
            "in_flight_ticker": self._ladder_in_flight_ticker,
        }

    def record_entry_depth_observation(
        self, *, would_block: bool,
    ) -> None:
        """Phase 4 telemetry hook: record a hypothetical entry-depth
        rejection near expiry.

        ``would_block`` is True when the book thickness at the entry
        price was below the documented activation threshold. This is
        called at decision time by the coordinator; the entry itself
        proceeds regardless. Recording these helps decide whether
        actual gating would have prevented losses without sacrificing
        winners.
        """
        self._near_expiry_depth_observed += 1
        if would_block:
            self._near_expiry_depth_would_block += 1

    def get_phase4_telemetry(self) -> dict:
        """Read-only snapshot for the deferred-gate workstream."""
        return {
            "orphan_break_even_observed": self._orphan_break_even_observed,
            "orphan_break_even_blocked": self._orphan_break_even_blocked,
            "near_expiry_depth_observed": self._near_expiry_depth_observed,
            "near_expiry_depth_would_block": self._near_expiry_depth_would_block,
        }

    @staticmethod
    def _compute_expiry_retry_floor(
        side: str,
        attempt: int,
        cfg,
    ) -> int:
        """Compute the order-side floor (in cents) for an EXPIRY_GUARD
        / SHORT_SETTLEMENT_GUARD retry attempt.

        Defaults preserve legacy behavior (attempt 0 already uses the
        1-cent floor). Mechanics:

          * For a long exit (side == "yes" → sell YES) the floor STARTS
            at ``first_attempt_yes_floor_cents`` and steps DOWN by
            ``widen_step_cents`` each retry, clamped to [1, 99].
          * For a short exit (side == "no" → sell NO) the floor STARTS
            at ``first_attempt_no_floor_cents`` and steps DOWN the same
            way (lower floor = more aggressive on the NO side as well,
            since the order is also a sell with a price floor).
          * On the FINAL attempt (attempt == max_attempts - 1; i.e.
            the 0-based index of the last try in a sequence of
            ``max_attempts`` total) the floor collapses to 1 when
            ``final_attempt_max_aggressive`` is True. This is the
            BUG-032 safety rail -- we MUST be able to fall back to
            the existing aggressive behavior or we risk extending the
            orphan window beyond the contract close.
        """
        max_attempts = max(1, cfg.expiry_retry_max_attempts)
        widen = max(0, cfg.expiry_retry_widen_step_cents)
        if side == "yes":
            base = max(1, min(99, cfg.expiry_retry_first_attempt_yes_floor_cents))
        else:
            base = max(1, min(99, cfg.expiry_retry_first_attempt_no_floor_cents))
        # ``attempt`` is the 0-based retry index from the coordinator
        # loop; the final attempt is ``max_attempts - 1``.
        if (cfg.expiry_retry_final_attempt_max_aggressive
                and attempt >= max_attempts - 1):
            return 1
        floor = base - widen * max(0, attempt)
        return max(1, min(99, floor))

    async def _exit_inner(
        self,
        price: float,
        reason: str,
        attempt: int = 0,
    ) -> Optional[dict]:
        """Exit logic without acquiring the lock (caller must hold it).

        Phase 2: ``attempt`` selects the order-side floor for
        EXPIRY_GUARD / SHORT_SETTLEMENT_GUARD retries via
        ``_compute_expiry_retry_floor``. All other reasons use the
        legacy 1-cent floor unconditionally.
        """
        if self.position is None:
            return None

        pos = self.position
        self._transition(PositionState.EXITING)

        side = "yes" if pos.direction == "long" else "no"
        client_order_id = self._generate_client_order_id(pos.ticker, "sell")

        # Phase 2: only widen for the two close-race reasons. Every
        # other exit (STOP_LOSS, TAKE_PROFIT, MOMENTUM_STALL, manual,
        # emergency_close, ...) must keep the legacy 1-cent floor so
        # we don't accidentally turn a normal exit into a partial fill.
        expiry_reasons = ("EXPIRY_GUARD", "SHORT_SETTLEMENT_GUARD")
        if reason in expiry_reasons:
            floor_cents = self._compute_expiry_retry_floor(
                side=side, attempt=attempt, cfg=settings.bot,
            )
        else:
            floor_cents = 1
        price_kwargs = (
            {"yes_price": floor_cents} if side == "yes"
            else {"no_price": floor_cents}
        )

        exit_order_id = None
        try:
            result = await self.client.create_order(
                ticker=pos.ticker,
                side=side,
                action="sell",
                count=pos.contracts,
                type="market",
                client_order_id=client_order_id,
                **price_kwargs,
            )
            if reason in expiry_reasons:
                logger.info(
                    "position_manager.expiry_retry_floor",
                    ticker=pos.ticker, side=side, attempt=attempt,
                    floor_cents=floor_cents, reason=reason,
                )
            exit_order_id = result.get("order", {}).get("order_id")
            logger.info("position_manager.exit_order_placed",
                        ticker=pos.ticker, order_id=exit_order_id)
        except Exception as e:
            error_str = str(e)
            is_conflict = "409" in error_str or "Conflict" in error_str
            logger.error("position_manager.exit_order_failed",
                         error=error_str, ticker=pos.ticker,
                         is_conflict=is_conflict)

            if is_conflict:
                settled = await self._check_if_market_settled(pos.ticker)
                if settled is not None:
                    logger.info("position_manager.exit_conflict_settled",
                                ticker=pos.ticker, result=settled)
                    self._transition(PositionState.OPEN)
                    return {"_settled": True, "_result": settled, "_via_409": True}

            recovered = await self._recover_order_after_failure(client_order_id)
            if recovered:
                exit_order_id = recovered.get("order_id")
                logger.warning("position_manager.exit_order_recovered",
                               order_id=exit_order_id)
            else:
                self._transition(PositionState.OPEN)
                return None

        exit_price = price
        exited_contracts = 0
        actual_fees = None
        exit_cost = None
        exit_fill_source = "order_response"
        if exit_order_id:
            # BUG-032: when the exit is racing the contract close
            # (EXPIRY_GUARD or SHORT_SETTLEMENT_GUARD), use a much shorter
            # fill-poll timeout so we get a chance to retry before the
            # contract resolves on the exchange. Default 5s vs 15s
            # general-purpose timeout.
            try:
                from config.settings import settings as _settings
                expiry_reasons = ("EXPIRY_GUARD", "SHORT_SETTLEMENT_GUARD")
                fill_timeout = (
                    _settings.bot.expiry_guard_fill_poll_timeout_sec
                    if reason in expiry_reasons else None
                )
            except Exception:
                fill_timeout = None
            order_data = await self._poll_order_fill(
                exit_order_id, timeout_sec=fill_timeout,
            )
            status = order_data.get("status", "")
            filled_count = self._parse_fill_count(order_data)

            if status == "canceled" and filled_count == 0:
                logger.warning("position_manager.exit_canceled",
                               ticker=pos.ticker, order_id=exit_order_id)
                self._transition(PositionState.OPEN)
                return None

            if filled_count > 0:
                exited_contracts = filled_count

            parsed_price = self._parse_fill_price_yes_side(order_data)
            if parsed_price is not None:
                exit_price = parsed_price

            actual_fees = self._parse_actual_fees(order_data)
            exit_cost = self._parse_fill_cost(order_data)

            # BUG-025: prefer the WS Fill events over the polled order's
            # quoted price/cost/fees. See enter() for the rationale and
            # fallback semantics.
            ws_fills, exit_fill_source = await self._drain_fill_stream(
                exit_order_id,
                min_count=exited_contracts or pos.contracts,
                leg="exit",
            )
            if ws_fills:
                ws_vwap = FillStream.vwap_yes_cents(ws_fills)
                if ws_vwap is not None:
                    exit_price = ws_vwap
                exit_cost = FillStream.total_cost_dollars(ws_fills)
                actual_fees = FillStream.total_fees_dollars(ws_fills)

        # Ledger lag: wait before verification so Kalshi's position
        # endpoint reflects the fill that just occurred (Fix 2).
        await asyncio.sleep(1.5)

        remaining_signed = await self.verify_position_on_exchange(pos.ticker)
        if remaining_signed == VERIFY_FAILED:
            if exited_contracts == 0:
                logger.error("position_manager.exit_unverifiable",
                             ticker=pos.ticker, order_id=exit_order_id)
                self._transition(PositionState.OPEN)
                return None
            logger.warning("position_manager.exit_verify_failed_trusting_poll",
                           ticker=pos.ticker, filled=exited_contracts)
        else:
            expected_sign = 1 if pos.direction == "long" else -1
            remaining_same_side = remaining_signed * expected_sign

            if remaining_same_side < 0:
                logger.error("position_manager.exit_overshot_to_opposite_side",
                             ticker=pos.ticker,
                             exchange_position=remaining_signed,
                             expected_direction=pos.direction)
                exited_contracts = pos.contracts
            elif remaining_same_side >= pos.contracts:
                logger.error("position_manager.phantom_exit_prevented",
                             ticker=pos.ticker,
                             exchange_position=remaining_signed)
                self._transition(PositionState.OPEN)
                return None
            else:
                verified_exited = pos.contracts - remaining_same_side
                if exited_contracts == 0:
                    exited_contracts = verified_exited
                elif verified_exited < exited_contracts:
                    logger.warning("position_manager.exit_fill_mismatch",
                                   poll_filled=exited_contracts,
                                   exchange_exited=verified_exited)
                    exited_contracts = verified_exited

        if exited_contracts == 0:
            logger.error("position_manager.exit_zero_fill",
                         ticker=pos.ticker, order_id=exit_order_id)
            self._transition(PositionState.OPEN)
            return None

        # Handle partial exit
        if exited_contracts < pos.contracts:
            remainder = pos.contracts - exited_contracts
            logger.warning("position_manager.partial_exit",
                           ticker=pos.ticker, filled=exited_contracts,
                           remainder=remainder)
            self.adopt_orphan(pos.ticker, pos.direction, remainder, pos.entry_price)
            self._transition(PositionState.PARTIAL_EXIT)

        # Build trade result -- cash-flow PnL from actual Kalshi dollar
        # amounts. ``entry_cost`` is what we paid to open (positive
        # outflow). ``exit_cost`` is what we *received* selling the
        # position back -- ``FillStream.total_cost_dollars`` is unsigned
        # for both buys and sells, and the corresponding ``taker_fill_
        # cost_dollars`` field on a sell-side polled order is also the
        # proceeds. So a mid-flight exit is:
        #
        #     PnL = exit_proceeds - entry_paid - all_fees
        #         = exit_cost     - entry_cost - (entry_fees + exit_fees)
        #
        # The contract's $1.00 max payout never enters the math here --
        # that's only relevant at settlement, which is handled by
        # ``_handle_settlement_inner`` with its own (correct) formula.
        # BUG-027: the previous version used
        # ``contracts*$1.00 - entry_cost - exit_cost - fees`` which (a)
        # invented a payout the trade never received and (b) treated
        # the sale proceeds as an additional outflow, over-recording
        # PnL by roughly ``2 * exit_cost`` on every LONG mid-flight exit.
        entry_cost = pos.entry_cost_dollars
        if entry_cost is not None and exit_cost is not None:
            entry_fees = pos.entry_fees_dollars or 0.0
            exit_fees = actual_fees or 0.0
            total_fees = entry_fees + exit_fees
            net_pnl = exit_cost - entry_cost - total_fees
            fees = total_fees
            notional = entry_cost if entry_cost > 0 else 1.0
            pnl_pct = net_pnl / notional if notional > 0 else 0
            logger.info("position_manager.pnl_cost_based",
                        exited_contracts=exited_contracts,
                        entry_cost=entry_cost, exit_cost=exit_cost,
                        entry_fees=entry_fees, exit_fees=exit_fees,
                        net_pnl=net_pnl)
        else:
            d = 1 if pos.direction == "long" else -1
            pnl_per_contract = d * (exit_price - pos.entry_price) / 100
            gross_pnl = pnl_per_contract * exited_contracts
            notional = exited_contracts * pos.entry_price / 100
            if actual_fees is not None:
                fees = actual_fees
            else:
                fees = notional * 0.007
            net_pnl = gross_pnl - fees
            pnl_pct = net_pnl / notional if notional > 0 else 0
            logger.warning("position_manager.pnl_formula_fallback",
                           entry_cost_available=entry_cost is not None,
                           exit_cost_available=exit_cost is not None)

        trade_result = {
            "ticker": pos.ticker,
            "direction": pos.direction,
            "contracts": exited_contracts,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "pnl": round(net_pnl, 4),
            "pnl_pct": round(pnl_pct, 4),
            "fees": round(fees, 4),
            "exit_reason": reason,
            "conviction": pos.conviction,
            "regime_at_entry": pos.regime_at_entry,
            "candles_held": pos.candles_held,
            "entry_time": pos.entry_time,
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "entry_order_id": pos.order_id,
            "exit_order_id": exit_order_id,
            "entry_obi": pos.entry_obi,
            "entry_roc": pos.entry_roc,
            "signal_driver": pos.signal_driver,
            "max_favorable_excursion": pos.max_favorable_excursion,
            "max_adverse_excursion": pos.max_adverse_excursion,
            # BUG-025 reconciliation context. Coordinator persists these
            # alongside the trade row so analytics can quantify how often
            # the WS path engaged and how big any remaining cost-vs-wallet
            # drift was.
            "entry_cost_dollars": pos.entry_cost_dollars,
            "exit_cost_dollars": exit_cost,
            "entry_fill_source": pos.entry_fill_source,
            "exit_fill_source": exit_fill_source,
            "wallet_at_entry": pos.wallet_at_entry,
        }

        self.position = None
        self._transition(PositionState.FLAT)
        self._completed_live_trades += 1
        self._record_exit_cooldown(trade_result["ticker"])

        logger.info("position_manager.exit_confirmed",
                     ticker=trade_result["ticker"],
                     contracts=exited_contracts,
                     pnl=trade_result["pnl"], reason=reason,
                     completed_trades=self._completed_live_trades,
                     trade_limit=self.live_trade_limit,
                     entry_fill_source=pos.entry_fill_source,
                     exit_fill_source=exit_fill_source)
        return trade_result

    # ── SETTLEMENT ────────────────────────────────────────────────────

    async def handle_settlement(self, result: str, *, via_409: bool = False) -> Optional[dict]:
        """Handle contract settlement. Acquires lock, verifies with exchange.

        ``via_409`` is True when settlement was reached via a 409 Conflict on
        an exit attempt (the contract was already closing on the exchange).
        Used to disambiguate ``EXPIRY_409_SETTLED`` from a normal settlement.
        """
        async with self._lock:
            return await self._handle_settlement_inner(result, via_409=via_409)

    async def _handle_settlement_inner(self, result: str, *, via_409: bool = False) -> Optional[dict]:
        """Settlement logic without lock (caller must hold it)."""
        if self.position is None:
            return None

        # BUG-022 fix: refuse to settle on an ambiguous result. Empty string or
        # any non-yes/no value silently mapped to "no" in the old code, which
        # turned wins into recorded losses whenever settlement fired before
        # Kalshi finalized the market. Caller must retry later with a real
        # "yes"/"no".
        if result not in ("yes", "no"):
            logger.critical(
                "position_manager.settlement_invalid_result",
                ticker=self.position.ticker,
                result_raw=result,
            )
            return None

        pos = self.position
        settled_price = 100 if result == "yes" else 0

        settled_contracts = pos.contracts

        def _calc_settlement_pnl(contracts: int) -> tuple:
            """Compute settlement PnL. At settlement there is no exit cost --
            Kalshi either pays $1/contract (win) or $0 (loss). If we have the
            original entry_cost_dollars we use it directly; otherwise fall back
            to the price-based formula."""
            entry_cost = pos.entry_cost_dollars
            if entry_cost is not None:
                won = (pos.direction == "long" and result == "yes") or \
                      (pos.direction == "short" and result == "no")
                payout = float(contracts) if won else 0.0
                entry_fees = pos.entry_fees_dollars or 0.0
                net = payout - entry_cost - entry_fees
                notional = entry_cost if entry_cost > 0 else 1.0
                pct = net / notional if notional > 0 else 0
                return net, pct, entry_fees
            d = 1 if pos.direction == "long" else -1
            pnl_per = d * (settled_price - pos.entry_price) / 100
            gross = pnl_per * contracts
            notional = contracts * pos.entry_price / 100
            f = notional * 0.007
            net = gross - f
            pct = net / notional if notional > 0 else 0
            return net, pct, f

        net_pnl, pnl_pct, fees = _calc_settlement_pnl(settled_contracts)

        # When settlement was reached via a 409 Conflict on an exit, label
        # the trade EXPIRY_409_SETTLED so it's distinguishable from a clean
        # contract settlement. Both paths are legitimate; the disambiguation
        # only matters for analytics and orphan-vs-expiry attribution.
        base_exit_reason = "EXPIRY_409_SETTLED" if via_409 else "CONTRACT_SETTLED"

        trade_result = {
            "ticker": pos.ticker,
            "direction": pos.direction,
            "contracts": settled_contracts,
            "entry_price": pos.entry_price,
            "exit_price": settled_price,
            "pnl": round(net_pnl, 4),
            "pnl_pct": round(pnl_pct, 4),
            "fees": round(fees, 4),
            "exit_reason": base_exit_reason,
            "conviction": pos.conviction,
            "regime_at_entry": pos.regime_at_entry,
            "candles_held": pos.candles_held,
            "entry_time": pos.entry_time,
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "entry_order_id": pos.order_id,
            "exit_order_id": None,
            "entry_obi": pos.entry_obi,
            "entry_roc": pos.entry_roc,
            "signal_driver": pos.signal_driver,
            "max_favorable_excursion": pos.max_favorable_excursion,
            "max_adverse_excursion": pos.max_adverse_excursion,
            # BUG-025: settlement has no exit fills; cost is simply not
            # populated and ``exit_fill_source`` is "settlement" so the
            # coordinator's reconciliation skips the cost diff for these.
            "entry_cost_dollars": pos.entry_cost_dollars,
            "exit_cost_dollars": None,
            "entry_fill_source": pos.entry_fill_source,
            "exit_fill_source": "settlement",
            "wallet_at_entry": pos.wallet_at_entry,
        }

        remaining = await self._verify_with_retry(pos.ticker)
        if remaining == VERIFY_FAILED:
            self._settled_tickers.add(pos.ticker)
            self._record_exit_cooldown(pos.ticker)
            self.position = None
            self._transition(PositionState.FLAT)
            self._completed_live_trades += 1
            trade_result["exit_reason"] = "CONTRACT_SETTLED_VERIFY_FAILED"
            logger.warning("position_manager.settlement_verify_failed_no_orphan",
                           ticker=pos.ticker)
            return trade_result

        remaining_abs = abs(remaining) if remaining != 0 else 0
        if remaining_abs > 0:
            logger.error("position_manager.settlement_position_still_open",
                         ticker=pos.ticker, remaining=remaining,
                         via_409=via_409)
            # Tag the orphan with the upstream cause so check_orphans can
            # later record it as EXPIRY_409_SETTLED instead of ORPHAN_SETTLED.
            # When this is the full-position redirect (remaining_abs >=
            # pos.contracts) we also pre-flag the orphan as counted because
            # we bump _completed_live_trades immediately below; without
            # this the orphan-recovery path would double-count.
            preflag_counted = remaining_abs >= pos.contracts
            self.adopt_orphan(pos.ticker, pos.direction,
                              remaining_abs, pos.entry_price,
                              cause="EXPIRY_409" if via_409 else None,
                              counted=preflag_counted)
            if remaining_abs >= pos.contracts:
                self.position = None
                self._transition(PositionState.FLAT)
                # Fix C: the round-trip ended (orphan path will close the
                # position), so the supervised counter must advance even
                # though we return None here. Otherwise the bot keeps
                # accepting new live entries despite hitting the limit.
                self._completed_live_trades += 1
                logger.info(
                    "position_manager.settlement_orphan_redirect_counted",
                    ticker=pos.ticker,
                    completed_trades=self._completed_live_trades,
                    trade_limit=self.live_trade_limit,
                    via_409=via_409,
                )
                return None
            settled_contracts = pos.contracts - remaining_abs
            trade_result["contracts"] = settled_contracts
            net_pnl, pnl_pct, fees = _calc_settlement_pnl(settled_contracts)
            trade_result["pnl"] = round(net_pnl, 4)
            trade_result["pnl_pct"] = round(pnl_pct, 4)
            trade_result["fees"] = round(fees, 4)

        self._settled_tickers.add(pos.ticker)
        self._record_exit_cooldown(pos.ticker)
        self.position = None
        self._transition(PositionState.FLAT)
        self._completed_live_trades += 1
        logger.info("position_manager.settlement_trade_counted",
                     completed_trades=self._completed_live_trades,
                     trade_limit=self.live_trade_limit,
                     via_409=via_409)
        return trade_result

    # ── ORPHAN MANAGEMENT ─────────────────────────────────────────────

    def adopt_orphan(self, ticker: str, direction: str, contracts: int,
                     avg_entry_price: float, *,
                     cause: Optional[str] = None,
                     counted: bool = False) -> None:
        already = any(o.ticker == ticker for o in self.orphaned_positions)
        if already:
            for o in self.orphaned_positions:
                if o.ticker == ticker:
                    if o.contracts != contracts:
                        logger.warning("position_manager.orphan_count_replaced",
                                       ticker=ticker, old=o.contracts,
                                       new=contracts)
                        o.contracts = contracts
                    else:
                        logger.debug("position_manager.orphan_unchanged",
                                     ticker=ticker, contracts=contracts)
                    # Upgrade the cause if a more specific one arrives. Don't
                    # downgrade EXPIRY_409 → None; the original tag wins.
                    if cause and not o.cause:
                        o.cause = cause
                    # ``counted`` is sticky: once set, never clear it.
                    if counted and not o.counted:
                        o.counted = True
            return
        orphan = OrphanedPosition(
            ticker=ticker,
            direction=direction,
            contracts=contracts,
            avg_entry_price=avg_entry_price,
            detected_at=datetime.now(timezone.utc).isoformat(),
            cause=cause,
            counted=counted,
        )
        self.orphaned_positions.append(orphan)
        logger.warning("position_manager.orphan_adopted",
                        ticker=ticker, direction=direction,
                        contracts=contracts, cause=cause, counted=counted)
        asyncio.ensure_future(self._persist_state())

    def adopt_orphan_and_clear_position(
        self, ticker: str, direction: str, contracts: int,
        avg_entry_price: float, *,
        cause: Optional[str] = None,
        counted: bool = False,
    ) -> None:
        """BUG-031 runtime fix: atomic adopt-orphan + clear position +
        transition to FLAT.

        The previous coordinator path was three separate mutations:
          ``pm.adopt_orphan(...)`` schedules a persist with the old
          ``state == EXITING`` and the old ``position`` still attached;
          ``pm.position = None`` clears the slot but does NOT call
          ``_transition``, so ``state`` stays ``EXITING``;
          a subsequent ``_transition`` from elsewhere (or the next
          ad-hoc ``_persist_state``) writes the inconsistent
          ``state="OPEN"|"EXITING"`` with ``position=null`` snapshot
          to disk.
        On the next restart, ``restore_from_snapshot`` reconciles via the
        BUG-031 startup fix, but during *runtime* the inconsistency
        still leaks into ``can_enter`` (which trusts ``self.state``)
        and the live lane silently goes dark.

        This method does all three mutations under no async barrier and
        schedules exactly one ``_persist_state`` at the end so the
        snapshot can never observe an in-between state.
        """
        self.adopt_orphan(
            ticker, direction, contracts, avg_entry_price,
            cause=cause, counted=counted,
        )
        self.position = None
        if self.state != PositionState.FLAT:
            old = self.state
            self.state = PositionState.FLAT
            logger.info(
                "position_manager.state_transition",
                old=old.value, new=PositionState.FLAT.value,
                ticker=ticker, source="adopt_orphan_and_clear_position",
            )
        asyncio.ensure_future(self._persist_state())

    async def check_orphans(self) -> list[dict]:
        """Check orphaned positions for recovery. Acquires lock."""
        async with self._lock:
            return await self._check_orphans_inner()

    async def _check_orphans_inner(self) -> list[dict]:
        closed = []
        remaining_list = []

        for orphan in self.orphaned_positions:
            try:
                market_data = await self.client.get_market(orphan.ticker)
                market = market_data.get("market", {})
                status = market.get("status", "")

                if status in ("closed", "settled", "finalized"):
                    result = market.get("result", "")
                    # BUG-022 fix: Kalshi reports status="closed" immediately at
                    # close_time but takes ~2-3 min to finalize the result. If
                    # we settle during that window, result="" defaults to "no"
                    # which silently records wins as losses. Wait for a real
                    # "yes"/"no" before computing PnL.
                    if result not in ("yes", "no"):
                        logger.info(
                            "position_manager.orphan_awaiting_finalization",
                            ticker=orphan.ticker,
                            status=status,
                            result_raw=result,
                        )
                        remaining_list.append(orphan)
                        continue
                    settled_price = 100 if result == "yes" else 0
                    d = 1 if orphan.direction == "long" else -1
                    pnl_per = d * (settled_price - orphan.avg_entry_price) / 100
                    gross = pnl_per * orphan.contracts
                    fees = (orphan.contracts * orphan.avg_entry_price / 100) * 0.007
                    # If the orphan originated from a 409-Conflict exit at
                    # expiry, label it EXPIRY_409_SETTLED so analytics can
                    # separate true phantom-fill orphans from expiry-time
                    # exit conflicts (the latter are not bugs).
                    settled_reason = (
                        "EXPIRY_409_SETTLED"
                        if orphan.cause == "EXPIRY_409"
                        else "ORPHAN_SETTLED"
                    )
                    closed.append({
                        "ticker": orphan.ticker,
                        "direction": orphan.direction,
                        "contracts": orphan.contracts,
                        "entry_price": orphan.avg_entry_price,
                        "exit_price": settled_price,
                        "pnl": round(gross - fees, 4),
                        "reason": settled_reason,
                        # Surface the counted flag so the coordinator only
                        # advances the supervised counter for orphans that
                        # weren't already counted by the settlement path.
                        "already_counted": orphan.counted,
                    })
                    self._settled_tickers.add(orphan.ticker)
                    continue

                if orphan.direction == "long":
                    bid = market.get("yes_bid")
                else:
                    bid = market.get("no_bid")

                # Phase 4: telemetry-only break-even-gate observation.
                # Increment ``observed`` whenever we have a usable bid,
                # ``blocked`` when the bid is under entry. Never changes
                # behavior; we still only act on bid >= entry below.
                if bid is not None:
                    self._orphan_break_even_observed += 1
                    if bid < orphan.avg_entry_price:
                        self._orphan_break_even_blocked += 1

                if bid is not None and bid >= orphan.avg_entry_price:
                    side = "yes" if orphan.direction == "long" else "no"
                    yes_price = int(bid) if side == "yes" else None
                    no_price = int(100 - bid) if side == "no" else None
                    client_order_id = self._generate_client_order_id(
                        orphan.ticker, "orphan-sell"
                    )
                    try:
                        result = await self.client.create_order(
                            ticker=orphan.ticker, side=side,
                            action="sell", count=orphan.contracts,
                            type="market",
                            yes_price=yes_price, no_price=no_price,
                            client_order_id=client_order_id,
                        )
                        order_id = result.get("order", {}).get("order_id")
                        filled = 0
                        actual_price = bid
                        orphan_exit_cost = None
                        orphan_fees = None
                        orphan_fill_source = "order_response"
                        if order_id:
                            order_data = await self._poll_order_fill(order_id)
                            filled = self._parse_fill_count(order_data)
                            parsed = self._parse_fill_price_yes_side(order_data)
                            if parsed is not None:
                                actual_price = parsed
                            orphan_exit_cost = self._parse_fill_cost(order_data)
                            orphan_fees = self._parse_actual_fees(order_data)

                            # BUG-025: same WS-first override as the main
                            # exit path. Orphan recoveries are infrequent
                            # but every dollar of price accuracy matters
                            # for attribution.
                            ws_fills, orphan_fill_source = await self._drain_fill_stream(
                                order_id,
                                min_count=filled or orphan.contracts,
                                leg="orphan",
                            )
                            if ws_fills:
                                ws_vwap = FillStream.vwap_yes_cents(ws_fills)
                                if ws_vwap is not None:
                                    actual_price = ws_vwap
                                orphan_exit_cost = FillStream.total_cost_dollars(ws_fills)
                                orphan_fees = FillStream.total_fees_dollars(ws_fills)

                        if filled == 0:
                            remaining = await self.verify_position_on_exchange(
                                orphan.ticker
                            )
                            remaining_abs = abs(remaining) if remaining != VERIFY_FAILED else 0
                            if remaining == VERIFY_FAILED or remaining_abs >= orphan.contracts:
                                remaining_list.append(orphan)
                                continue
                            filled = orphan.contracts - remaining_abs

                        if orphan_exit_cost is not None:
                            entry_est = orphan.contracts * orphan.avg_entry_price / 100
                            gross = orphan_exit_cost - entry_est
                            fees = orphan_fees if orphan_fees is not None else 0.0
                        else:
                            d = 1 if orphan.direction == "long" else -1
                            pnl_per = d * (actual_price - orphan.avg_entry_price) / 100
                            gross = pnl_per * filled
                            fees = (filled * orphan.avg_entry_price / 100) * 0.007
                        closed.append({
                            "ticker": orphan.ticker,
                            "direction": orphan.direction,
                            "contracts": filled,
                            "entry_price": orphan.avg_entry_price,
                            "exit_price": actual_price,
                            "pnl": round(gross - fees, 4),
                            "reason": "ORPHAN_RECOVERY",
                            "order_id": order_id,
                            "already_counted": orphan.counted,
                            "exit_fill_source": orphan_fill_source,
                        })

                        unfilled = orphan.contracts - filled
                        if unfilled > 0:
                            remaining_list.append(OrphanedPosition(
                                ticker=orphan.ticker,
                                direction=orphan.direction,
                                contracts=unfilled,
                                avg_entry_price=orphan.avg_entry_price,
                                detected_at=orphan.detected_at,
                                cause=orphan.cause,
                                # The original orphan was counted (or not)
                                # for the round-trip. The remainder is the
                                # same logical round-trip, so the flag must
                                # carry over to the next check_orphans call
                                # — otherwise we'd double-count when the
                                # remainder closes too.
                                counted=True,
                            ))
                        continue
                    except Exception as e:
                        logger.error("position_manager.orphan_exit_failed",
                                     ticker=orphan.ticker, error=str(e))

                remaining_list.append(orphan)
            except Exception as e:
                logger.error("position_manager.orphan_check_failed",
                             ticker=orphan.ticker, error=str(e))
                remaining_list.append(orphan)

        self.orphaned_positions = remaining_list
        asyncio.ensure_future(self._persist_state())
        return closed

    # ── RECONCILIATION ────────────────────────────────────────────────

    async def reconcile(self, our_series_prefixes: tuple[str, ...] = ("KXBTC", "KXETH")) -> None:
        """Bidirectional reconciliation with Kalshi exchange. Acquires lock."""
        async with self._lock:
            await self._reconcile_inner(our_series_prefixes)

    async def _reconcile_inner(
        self,
        our_series_prefixes: tuple[str, ...] = ("KXBTC", "KXETH"),
    ) -> None:
        try:
            positions_data = await self.client.get_positions(count_filter="position")
            market_positions = positions_data.get("market_positions", [])
        except Exception as e:
            logger.warning("position_manager.reconcile_fetch_failed", error=str(e))
            return

        tracked_ticker = self.position.ticker if self.position else None
        exchange_tickers: set[str] = set()

        for mp in market_positions:
            raw_position = float(mp.get("position_fp", 0))
            contracts = abs(int(raw_position))
            if contracts == 0:
                continue

            ticker = mp.get("ticker", "")
            is_ours = any(ticker.startswith(p) for p in our_series_prefixes)
            if not is_ours:
                continue

            exchange_tickers.add(ticker)
            direction = "long" if raw_position > 0 else "short"
            total_cost = float(mp.get("total_traded_dollars", 0))
            avg_entry_cents = round((total_cost / contracts) * 100) if contracts > 0 else 0

            if ticker == tracked_ticker:
                if self.position and contracts != self.position.contracts:
                    logger.warning("position_manager.reconcile_count_mismatch",
                                   ticker=ticker,
                                   bot=self.position.contracts,
                                   exchange=contracts)
                    self.position.contracts = contracts
                continue

            already = any(o.ticker == ticker for o in self.orphaned_positions)
            if not already:
                if ticker in self._settled_tickers:
                    logger.debug("position_manager.reconcile_skip_settled", ticker=ticker)
                    continue
                if self._is_in_cooldown(ticker):
                    logger.info("position_manager.reconcile_skip_cooldown",
                                ticker=ticker)
                    continue
                try:
                    market_data = await self.client.get_market(ticker)
                    market_status = market_data.get("market", {}).get("status", "")
                    if market_status in ("closed", "settled", "finalized"):
                        logger.info("position_manager.reconcile_skip_settled_market",
                                    ticker=ticker, status=market_status)
                        self._settled_tickers.add(ticker)
                        continue
                except Exception as e:
                    logger.warning("position_manager.reconcile_market_check_failed",
                                   ticker=ticker, error=str(e))
                max_sane = settings.risk.max_live_contracts * 3
                if contracts > max_sane:
                    logger.critical(
                        "position_manager.oversized_orphan_detected",
                        ticker=ticker, contracts=contracts, max_sane=max_sane)
                    try:
                        side = "yes" if direction == "long" else "no"
                        await self.client.create_order(
                            ticker=ticker, side=side,
                            action="sell", count=contracts,
                            order_type="market",
                        )
                        logger.info("position_manager.oversized_orphan_closed",
                                    ticker=ticker, contracts=contracts)
                        continue
                    except Exception as close_err:
                        logger.error(
                            "position_manager.oversized_orphan_close_failed",
                            ticker=ticker, error=str(close_err))

                self.adopt_orphan(ticker, direction, contracts, avg_entry_cents)

        if tracked_ticker and tracked_ticker not in exchange_tickers:
            if self.state not in (PositionState.ENTERING, PositionState.EXITING):
                logger.critical("position_manager.ghost_position_detected",
                                ticker=tracked_ticker,
                                bot_contracts=self.position.contracts if self.position else 0)
                self.position = None
                self._transition(PositionState.FLAT)

        if self.state == PositionState.DESYNC:
            if not self.has_orphans and not self.has_position:
                self._transition(PositionState.FLAT)
            elif self.has_position:
                self._transition(PositionState.OPEN)

    # ── EMERGENCY CLOSE ───────────────────────────────────────────────

    async def emergency_close(self) -> Optional[dict]:
        """Force-close the current position. Acquires lock with retries."""
        async with self._lock:
            if self.position is None:
                return None

            MAX_RETRIES = 3
            for attempt in range(1, MAX_RETRIES + 1):
                if self.position is None:
                    return None
                try:
                    result = await self._exit_inner(
                        self.position.entry_price, "EMERGENCY_STOP"
                    )
                    if result:
                        return result
                except Exception as e:
                    logger.error("position_manager.emergency_close_retry",
                                 attempt=attempt, error=str(e))
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(2 ** attempt)

            if self.position:
                pos = self.position
                # BUG-031 runtime fix: atomic adopt + clear + transition.
                self.adopt_orphan_and_clear_position(
                    pos.ticker, pos.direction,
                    pos.contracts, pos.entry_price,
                )
                logger.error("position_manager.emergency_close_abandoned",
                             ticker=pos.ticker)
            return None

    async def close_all_exchange_positions(self) -> list[dict]:
        """Query exchange for ALL positions and close them. Acquires lock.

        Fix 4: Orphans are cleared per-ticker only when fill is confirmed,
        not blanket-cleared at the end.
        """
        async with self._lock:
            results = []
            try:
                positions_data = await self.client.get_positions(count_filter="position")
            except Exception as e:
                logger.error("position_manager.close_all_fetch_failed", error=str(e))
                return results
            for mp in positions_data.get("market_positions", []):
                raw_position = float(mp.get("position_fp", 0))
                contracts = abs(int(raw_position))
                if contracts == 0:
                    continue
                ticker = mp.get("ticker", "")
                direction = "long" if raw_position > 0 else "short"
                side = "yes" if direction == "long" else "no"
                client_order_id = self._generate_client_order_id(ticker, "close-all")
                try:
                    result = await self.client.create_order(
                        ticker=ticker, side=side, action="sell", count=contracts,
                        type="market", client_order_id=client_order_id,
                        **({"yes_price": 1} if side == "yes" else {"no_price": 1}),
                    )
                    order_id = result.get("order", {}).get("order_id")
                    filled = 0
                    if order_id:
                        od = await self._poll_order_fill(order_id)
                        filled = self._parse_fill_count(od)
                    results.append({"ticker": ticker, "direction": direction,
                                    "contracts": contracts, "filled": filled,
                                    "order_id": order_id,
                                    "status": "closed" if filled > 0 else "unfilled"})
                    if filled > 0:
                        self.orphaned_positions = [
                            o for o in self.orphaned_positions if o.ticker != ticker
                        ]
                except Exception as e:
                    results.append({"ticker": ticker, "error": str(e), "status": "failed"})
            self.position = None
            self._transition(PositionState.FLAT)
            return results

    # ── STATE PERSISTENCE ─────────────────────────────────────────────

    def get_snapshot(self) -> dict:
        return {
            "state": self.state.value,
            "position": asdict(self.position) if self.position else None,
            "orphaned_positions": [asdict(o) for o in self.orphaned_positions],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "completed_live_trades": self._completed_live_trades,
            "live_trade_limit": self.live_trade_limit,
            "settled_tickers": list(self._settled_tickers),
        }

    def restore_from_snapshot(self, snapshot: dict) -> None:
        state_str = snapshot.get("state", "FLAT")
        try:
            self.state = PositionState(state_str)
        except ValueError:
            self.state = PositionState.FLAT

        # Defensive: a snapshot persisted by an older binary won't have the
        # newer dataclass fields (BUG-025: ``entry_fill_source``,
        # ``wallet_at_entry``). A snapshot persisted by a *newer* binary
        # could similarly carry keys we don't yet know about. Filter to
        # only fields the current dataclass declares so a rolling upgrade
        # never crashes restore_state().
        pos_data = snapshot.get("position")
        if pos_data:
            allowed = set(ManagedPosition.__dataclass_fields__.keys())
            filtered = {k: v for k, v in pos_data.items() if k in allowed}
            self.position = ManagedPosition(**filtered)
        else:
            self.position = None

        # BUG-031 (2026-05-03): a snapshot can land here with state="OPEN"
        # AND position=null when an exit happened but the post-exit save
        # was lost (OOM-kill / crash between flipping `self.position = None`
        # and `_persist_state()`). Without this reconciliation, restore
        # leaves the manager stuck in "OPEN with no position" -- can_enter()
        # returns False forever, the live lane silently goes dark, and no
        # alarm fires (signal_log shows no rejections because we never get
        # past the position-manager guard). Force back to FLAT and log so
        # the operator can audit. Inverse case (state=FLAT but position
        # present) is handled below by trusting the state field over the
        # phantom position.
        if self.state != PositionState.FLAT and self.position is None:
            logger.warning(
                "position_manager.restore_inconsistent_state_no_position",
                stale_state=self.state.value,
                forced_to="FLAT",
            )
            self.state = PositionState.FLAT
        elif self.state == PositionState.FLAT and self.position is not None:
            logger.warning(
                "position_manager.restore_inconsistent_flat_with_position",
                ticker=self.position.ticker,
                contracts=self.position.contracts,
                forced_position_to="None",
            )
            self.position = None

        self.orphaned_positions = []
        orphan_allowed = set(OrphanedPosition.__dataclass_fields__.keys())
        for o_data in snapshot.get("orphaned_positions", []):
            filtered = {k: v for k, v in o_data.items() if k in orphan_allowed}
            self.orphaned_positions.append(OrphanedPosition(**filtered))

        self._completed_live_trades = snapshot.get("completed_live_trades", 0)
        # live_trade_limit is now controlled by the code default (None =
        # unlimited). Ignore whatever the snapshot persisted so operators
        # don't have to wipe bot_state when changing the limit.

        restored_settled = snapshot.get("settled_tickers", [])
        self._settled_tickers = set(restored_settled)

        logger.info("position_manager.restored",
                     state=self.state.value,
                     has_position=self.has_position,
                     orphans=len(self.orphaned_positions),
                     settled_tickers=len(self._settled_tickers))

    def reset_trade_counter(self) -> None:
        """Reset the completed trade counter (e.g. after operator review)."""
        self._completed_live_trades = 0
        logger.info("position_manager.trade_counter_reset",
                     trade_limit=self.live_trade_limit)
        asyncio.ensure_future(self._persist_state())

    def bump_completed_trades(self, n: int = 1, *, source: str = "external") -> int:
        """Advance the supervised round-trip counter.

        The orphan-recovery path (``coordinator._check_orphaned_positions``)
        closes positions outside of ``exit()``/``handle_settlement``, so it
        needs an explicit way to tell the position manager that another
        live round-trip is now complete. Without this, the supervised
        ``live_trade_limit`` gate never trips for orphan-only round-trips,
        and the bot could silently keep entering past the configured limit.

        Returns the new total. ``n`` must be >= 0.
        """
        if n <= 0:
            return self._completed_live_trades
        self._completed_live_trades += n
        logger.info("position_manager.completed_trades_bumped",
                     source=source, increment=n,
                     completed_trades=self._completed_live_trades,
                     trade_limit=self.live_trade_limit)
        asyncio.ensure_future(self._persist_state())
        return self._completed_live_trades

    async def _persist_state(self) -> None:
        if self._db_pool is None:
            return
        try:
            snapshot = json.dumps(self.get_snapshot())
            async with self._db_pool.connection() as conn:
                await conn.execute(
                    """INSERT INTO bot_state (key, value, updated_at)
                       VALUES ('position_manager_state', %s::jsonb, NOW())
                       ON CONFLICT (key) DO UPDATE
                       SET value = EXCLUDED.value, updated_at = NOW()""",
                    (snapshot,),
                )
        except Exception as e:
            logger.error("position_manager.persist_failed", error=str(e))

    async def restore_state(self) -> None:
        if self._db_pool is None:
            return
        try:
            async with self._db_pool.connection() as conn:
                row = await conn.execute(
                    "SELECT value FROM bot_state WHERE key = 'position_manager_state'"
                )
                result = await row.fetchone()
            if result:
                data = result[0] if isinstance(result[0], dict) else json.loads(result[0])
                self.restore_from_snapshot(data)
        except Exception as e:
            logger.warning("position_manager.restore_failed", error=str(e))

    # ── STATUS ────────────────────────────────────────────────────────

    def get_state(self) -> dict:
        return {
            "state": self.state.value,
            "has_position": self.has_position,
            "is_busy": self.is_busy,
            "can_enter": self.can_enter,
            "position": {
                "ticker": self.position.ticker,
                "direction": self.position.direction,
                "contracts": self.position.contracts,
                "entry_price": self.position.entry_price,
                "candles_held": self.position.candles_held,
                "conviction": self.position.conviction,
                "signal_driver": self.position.signal_driver,
            } if self.position else None,
            "orphaned_positions": [
                {
                    "ticker": o.ticker,
                    "direction": o.direction,
                    "contracts": o.contracts,
                    "avg_entry_price": o.avg_entry_price,
                    "detected_at": o.detected_at,
                    "cause": o.cause,
                }
                for o in self.orphaned_positions
            ],
            "live_trade_limit": self.live_trade_limit,
            "completed_live_trades": self._completed_live_trades,
        }
