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

import structlog

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
    order_id: Optional[str] = None
    entry_cost_dollars: Optional[float] = None
    entry_fees_dollars: Optional[float] = None


@dataclass
class OrphanedPosition:
    ticker: str
    direction: str
    contracts: int
    avg_entry_price: float
    detected_at: str


# Kalshi canonical terminal statuses (API v2 spec 3.13.0)
TERMINAL_STATUSES = ("executed", "canceled")

FILL_POLL_INTERVAL = 0.25
FILL_POLL_TIMEOUT = 15.0
VERIFY_FAILED = -1


class PositionManager:
    """Thread-safe, exchange-anchored position lifecycle manager.

    All mutations (enter, exit, reconcile, emergency close) acquire self._lock
    so only one order operation can be in-flight at any time.
    """

    def __init__(self, client: KalshiOrderClient):
        self.client = client
        self._lock = asyncio.Lock()
        self.state = PositionState.FLAT
        self.position: Optional[ManagedPosition] = None
        self.orphaned_positions: list[OrphanedPosition] = []
        self._db_pool = None

        # Supervised single-trade mode: after each completed live round-trip,
        # the coordinator auto-pauses for post-trade review. Set to None for
        # unlimited (normal operation once testing is complete).
        self.live_trade_limit: Optional[int] = 1
        self._completed_live_trades: int = 0

        # Tracks tickers confirmed as settled/finalized to prevent
        # reconciliation from re-adopting them as orphans (BUG-008).
        self._settled_tickers: set[str] = set()

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

    async def _poll_order_fill(self, order_id: str) -> dict:
        elapsed = 0.0
        while elapsed < FILL_POLL_TIMEOUT:
            await asyncio.sleep(FILL_POLL_INTERVAL)
            elapsed += FILL_POLL_INTERVAL
            try:
                detail = await self.client.get_order(order_id)
                order_data = detail.get("order", {})
                status = order_data.get("status", "")
                if status in TERMINAL_STATUSES:
                    return order_data
            except Exception as e:
                logger.warning("position_manager.poll_order_error",
                               order_id=order_id, error=str(e), elapsed=elapsed)
        logger.warning("position_manager.poll_order_timeout",
                        order_id=order_id, timeout=FILL_POLL_TIMEOUT)
        try:
            detail = await self.client.get_order(order_id)
            return detail.get("order", {})
        except Exception:
            return {}

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

            # Pre-order: confirm exchange is flat
            if not await self._check_flat_on_exchange(ticker):
                self._transition(PositionState.DESYNC)
                return None

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

            # Poll for fill
            fill_price = price
            filled_contracts = 0
            entry_cost = None
            entry_fees = None
            if order_id:
                order_data = await self._poll_order_fill(order_id)
                status = order_data.get("status", "")
                filled_count = self._parse_fill_count(order_data)

                if status == "canceled" and filled_count == 0:
                    logger.warning("position_manager.entry_canceled",
                                   ticker=ticker, order_id=order_id)
                    self._transition(PositionState.FLAT)
                    return None

                if filled_count > 0:
                    filled_contracts = filled_count

                parsed_price = self._parse_fill_price_yes_side(order_data)
                if parsed_price is not None:
                    fill_price = parsed_price

                entry_cost = self._parse_fill_cost(order_data)
                entry_fees = self._parse_actual_fees(order_data)

            # Ledger lag: wait before verification so Kalshi's position
            # endpoint reflects the fill that just occurred (Fix 2).
            await asyncio.sleep(1.5)

            verified = await self.verify_position_on_exchange(ticker)
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
                            ticker=ticker)
                self._transition(PositionState.FLAT)
                return None
            else:
                filled_contracts = verified

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
            )
            self._transition(PositionState.OPEN)

            logger.info("position_manager.entry_confirmed",
                         ticker=ticker, direction=direction,
                         contracts=filled_contracts, price=fill_price)
            return self.position

    # ── EXIT ──────────────────────────────────────────────────────────

    async def exit(self, price: float, reason: str) -> Optional[dict]:
        """Place an exit order with full exchange verification.

        Returns a dict with trade details on success, None on failure.
        If a 409 Conflict reveals the market settled, delegates to
        handle_settlement automatically.
        Acquires the position lock.
        """
        async with self._lock:
            result = await self._exit_inner(price, reason)
            if result and result.get("_settled"):
                settled_result = result["_result"]
                logger.info("position_manager.exit_redirected_to_settlement",
                            result=settled_result)
                return await self._handle_settlement_inner(settled_result)
            return result

    async def _exit_inner(self, price: float, reason: str) -> Optional[dict]:
        """Exit logic without acquiring the lock (caller must hold it)."""
        if self.position is None:
            return None

        pos = self.position
        self._transition(PositionState.EXITING)

        side = "yes" if pos.direction == "long" else "no"
        client_order_id = self._generate_client_order_id(pos.ticker, "sell")

        exit_order_id = None
        try:
            result = await self.client.create_order(
                ticker=pos.ticker,
                side=side,
                action="sell",
                count=pos.contracts,
                type="market",
                client_order_id=client_order_id,
                **({"yes_price": 1} if side == "yes" else {"no_price": 1}),
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
                    return {"_settled": True, "_result": settled}

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
        if exit_order_id:
            order_data = await self._poll_order_fill(exit_order_id)
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

        # Build trade result -- prefer Kalshi's actual dollar costs over formula.
        # Each Kalshi binary contract pays out $1.00 max. Both entry and exit
        # taker_fill_cost_dollars represent money spent (entry=buy cost,
        # exit=close cost). PnL = max_payout - entry_cost - exit_cost - all_fees.
        entry_cost = pos.entry_cost_dollars
        if entry_cost is not None and exit_cost is not None:
            contracts_value = float(exited_contracts)
            entry_fees = pos.entry_fees_dollars or 0.0
            exit_fees = actual_fees or 0.0
            total_fees = entry_fees + exit_fees
            net_pnl = contracts_value - entry_cost - exit_cost - total_fees
            fees = total_fees
            notional = entry_cost if entry_cost > 0 else 1.0
            pnl_pct = net_pnl / notional if notional > 0 else 0
            logger.info("position_manager.pnl_cost_based",
                        contracts_value=contracts_value,
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
        }

        self.position = None
        self._transition(PositionState.FLAT)
        self._completed_live_trades += 1

        logger.info("position_manager.exit_confirmed",
                     ticker=trade_result["ticker"],
                     contracts=exited_contracts,
                     pnl=trade_result["pnl"], reason=reason,
                     completed_trades=self._completed_live_trades,
                     trade_limit=self.live_trade_limit)
        return trade_result

    # ── SETTLEMENT ────────────────────────────────────────────────────

    async def handle_settlement(self, result: str) -> Optional[dict]:
        """Handle contract settlement. Acquires lock, verifies with exchange."""
        async with self._lock:
            return await self._handle_settlement_inner(result)

    async def _handle_settlement_inner(self, result: str) -> Optional[dict]:
        """Settlement logic without lock (caller must hold it)."""
        if self.position is None:
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

        trade_result = {
            "ticker": pos.ticker,
            "direction": pos.direction,
            "contracts": settled_contracts,
            "entry_price": pos.entry_price,
            "exit_price": settled_price,
            "pnl": round(net_pnl, 4),
            "pnl_pct": round(pnl_pct, 4),
            "fees": round(fees, 4),
            "exit_reason": "CONTRACT_SETTLED",
            "conviction": pos.conviction,
            "regime_at_entry": pos.regime_at_entry,
            "candles_held": pos.candles_held,
            "entry_time": pos.entry_time,
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "entry_order_id": pos.order_id,
            "exit_order_id": None,
            "entry_obi": pos.entry_obi,
            "entry_roc": pos.entry_roc,
        }

        remaining = await self.verify_position_on_exchange(pos.ticker)
        if remaining == VERIFY_FAILED:
            self.adopt_orphan(pos.ticker, pos.direction,
                              pos.contracts, pos.entry_price)
            self.position = None
            self._transition(PositionState.FLAT)
            self._completed_live_trades += 1
            trade_result["exit_reason"] = "CONTRACT_SETTLED_VERIFY_FAILED"
            return trade_result

        remaining_abs = abs(remaining) if remaining != 0 else 0
        if remaining_abs > 0:
            logger.error("position_manager.settlement_position_still_open",
                         ticker=pos.ticker, remaining=remaining)
            self.adopt_orphan(pos.ticker, pos.direction,
                              remaining_abs, pos.entry_price)
            if remaining_abs >= pos.contracts:
                self.position = None
                self._transition(PositionState.FLAT)
                return None
            settled_contracts = pos.contracts - remaining_abs
            trade_result["contracts"] = settled_contracts
            net_pnl, pnl_pct, fees = _calc_settlement_pnl(settled_contracts)
            trade_result["pnl"] = round(net_pnl, 4)
            trade_result["pnl_pct"] = round(pnl_pct, 4)
            trade_result["fees"] = round(fees, 4)

        self.position = None
        self._transition(PositionState.FLAT)
        self._completed_live_trades += 1
        logger.info("position_manager.settlement_trade_counted",
                     completed_trades=self._completed_live_trades,
                     trade_limit=self.live_trade_limit)
        return trade_result

    # ── ORPHAN MANAGEMENT ─────────────────────────────────────────────

    def adopt_orphan(self, ticker: str, direction: str, contracts: int,
                     avg_entry_price: float) -> None:
        already = any(o.ticker == ticker for o in self.orphaned_positions)
        if already:
            for o in self.orphaned_positions:
                if o.ticker == ticker:
                    o.contracts += contracts
                    logger.warning("position_manager.orphan_updated",
                                   ticker=ticker, new_total=o.contracts)
            return
        orphan = OrphanedPosition(
            ticker=ticker,
            direction=direction,
            contracts=contracts,
            avg_entry_price=avg_entry_price,
            detected_at=datetime.now(timezone.utc).isoformat(),
        )
        self.orphaned_positions.append(orphan)
        logger.warning("position_manager.orphan_adopted",
                        ticker=ticker, direction=direction,
                        contracts=contracts)
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
                    settled_price = 100 if result == "yes" else 0
                    d = 1 if orphan.direction == "long" else -1
                    pnl_per = d * (settled_price - orphan.avg_entry_price) / 100
                    gross = pnl_per * orphan.contracts
                    fees = (orphan.contracts * orphan.avg_entry_price / 100) * 0.007
                    closed.append({
                        "ticker": orphan.ticker,
                        "direction": orphan.direction,
                        "contracts": orphan.contracts,
                        "entry_price": orphan.avg_entry_price,
                        "exit_price": settled_price,
                        "pnl": round(gross - fees, 4),
                        "reason": "ORPHAN_SETTLED",
                    })
                    self._settled_tickers.add(orphan.ticker)
                    continue

                if orphan.direction == "long":
                    bid = market.get("yes_bid")
                else:
                    bid = market.get("no_bid")

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
                        if order_id:
                            order_data = await self._poll_order_fill(order_id)
                            filled = self._parse_fill_count(order_data)
                            parsed = self._parse_fill_price_yes_side(order_data)
                            if parsed is not None:
                                actual_price = parsed
                            orphan_exit_cost = self._parse_fill_cost(order_data)
                            orphan_fees = self._parse_actual_fees(order_data)

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
                        })

                        unfilled = orphan.contracts - filled
                        if unfilled > 0:
                            remaining_list.append(OrphanedPosition(
                                ticker=orphan.ticker,
                                direction=orphan.direction,
                                contracts=unfilled,
                                avg_entry_price=orphan.avg_entry_price,
                                detected_at=orphan.detected_at,
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
                self.adopt_orphan(pos.ticker, pos.direction,
                                  pos.contracts, pos.entry_price)
                self.position = None
                self._transition(PositionState.FLAT)
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
        }

    def restore_from_snapshot(self, snapshot: dict) -> None:
        state_str = snapshot.get("state", "FLAT")
        try:
            self.state = PositionState(state_str)
        except ValueError:
            self.state = PositionState.FLAT

        pos_data = snapshot.get("position")
        if pos_data:
            self.position = ManagedPosition(**pos_data)
        else:
            self.position = None

        self.orphaned_positions = []
        for o_data in snapshot.get("orphaned_positions", []):
            self.orphaned_positions.append(OrphanedPosition(**o_data))

        self._completed_live_trades = snapshot.get("completed_live_trades", 0)
        restored_limit = snapshot.get("live_trade_limit")
        if restored_limit is not None:
            self.live_trade_limit = restored_limit

        logger.info("position_manager.restored",
                     state=self.state.value,
                     has_position=self.has_position,
                     orphans=len(self.orphaned_positions))

    def reset_trade_counter(self) -> None:
        """Reset the completed trade counter (e.g. after operator review)."""
        self._completed_live_trades = 0
        logger.info("position_manager.trade_counter_reset",
                     trade_limit=self.live_trade_limit)
        asyncio.ensure_future(self._persist_state())

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
            } if self.position else None,
            "orphaned_positions": [
                {
                    "ticker": o.ticker,
                    "direction": o.direction,
                    "contracts": o.contracts,
                    "avg_entry_price": o.avg_entry_price,
                    "detected_at": o.detected_at,
                }
                for o in self.orphaned_positions
            ],
            "live_trade_limit": self.live_trade_limit,
            "completed_live_trades": self._completed_live_trades,
        }
