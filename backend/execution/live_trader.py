"""
LiveTrader — real execution against the Kalshi API.

Delegates all position lifecycle management to PositionManager. LiveTrader
owns the PositionSizer, trade history recording, and provides backward-
compatible properties that the coordinator and routes expect.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import structlog

from config import settings

from data.fill_stream import FillStream
from data.kalshi_ws import KalshiOrderClient
from execution.position_manager import PositionManager, ManagedPosition
from risk.position_sizer import PositionSizer

logger = structlog.get_logger(__name__)


@dataclass
class LiveTrade:
    ticker: str
    direction: str
    contracts: int
    entry_price: float
    exit_price: float
    pnl: float
    pnl_pct: float
    fees: float
    exit_reason: str
    conviction: str
    regime_at_entry: str
    candles_held: int
    entry_time: datetime
    exit_time: datetime
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    entry_obi: float = 0.0
    entry_roc: float = 0.0
    signal_driver: str = "-"
    # BUG-025: reconciliation context propagated from PositionManager.
    entry_cost_dollars: Optional[float] = None
    exit_cost_dollars: Optional[float] = None
    entry_fill_source: str = "order_response"
    exit_fill_source: str = "order_response"
    wallet_at_entry: Optional[float] = None


class LiveTrader:
    FEE_RATE = 0.007

    def __init__(
        self,
        sizer: PositionSizer,
        fill_stream: Optional[FillStream] = None,
    ):
        self.sizer = sizer
        self.client = KalshiOrderClient()
        # BUG-025: optional fill-stream subscriber. Constructed by main()
        # alongside the live wiring; left None for paper/dev so unit tests
        # don't have to stand up a WebSocket auth flow.
        self.fill_stream = fill_stream
        self.position_manager = PositionManager(self.client, fill_stream=fill_stream)
        self.trades: list[LiveTrade] = []

    # ── Backward-compatible properties ────────────────────────────────

    @property
    def position(self) -> Optional[ManagedPosition]:
        return self.position_manager.position

    @position.setter
    def position(self, value) -> None:
        self.position_manager.position = value

    @property
    def has_position(self) -> bool:
        return self.position_manager.has_position

    @property
    def orphaned_positions(self):
        return self.position_manager.orphaned_positions

    @orphaned_positions.setter
    def orphaned_positions(self, value) -> None:
        self.position_manager.orphaned_positions = value

    def adopt_orphan(self, ticker: str, direction: str, contracts: int,
                     avg_entry_price: float) -> None:
        self.position_manager.adopt_orphan(ticker, direction, contracts, avg_entry_price)

    # ── Entry (delegates to PositionManager) ──────────────────────────

    async def enter(
        self,
        ticker: str,
        direction: str,
        price: float,
        conviction: str,
        regime: str,
        obi: float = 0.0,
        roc: float = 0.0,
        signal_driver: str = "-",
    ) -> Optional[ManagedPosition]:
        size_dollars = self.sizer.calculate_size(conviction, direction)
        cost_per = price / 100
        contracts = int(size_dollars / cost_per) if cost_per > 0 else 0
        if contracts < 1:
            logger.warning("live.position_too_small", size=size_dollars, price=price)
            return None

        max_contracts = settings.risk.max_live_contracts
        if contracts > max_contracts:
            logger.warning("live.contracts_capped",
                           raw=contracts, cap=max_contracts, price=price)
            contracts = max_contracts

        pos = await self.position_manager.enter(
            ticker=ticker,
            direction=direction,
            contracts=contracts,
            price=price,
            conviction=conviction,
            regime=regime,
            obi=obi,
            roc=roc,
            signal_driver=signal_driver,
        )

        if pos:
            logger.info("live.entry", ticker=ticker, direction=direction,
                         contracts=pos.contracts, price=pos.entry_price,
                         conviction=conviction)
        return pos

    # ── Exit (delegates to PositionManager, records trade) ────────────

    async def exit(self, price: float, reason: str) -> Optional[LiveTrade]:
        trade_result = await self.position_manager.exit(price, reason)
        if trade_result is None:
            return None

        trade = self._build_trade(trade_result)
        self.sizer.record_trade(trade.pnl)
        self.trades.append(trade)
        return trade

    # ── Settlement (delegates to PositionManager, records trade) ──────

    async def handle_settlement(self, result: str) -> Optional[LiveTrade]:
        trade_result = await self.position_manager.handle_settlement(result)
        if trade_result is None:
            return None

        trade = self._build_trade(trade_result)
        self.sizer.record_trade(trade.pnl)
        self.trades.append(trade)
        return trade

    # ── Orphan check (delegates to PositionManager) ───────────────────

    async def check_orphans(self) -> list[dict]:
        return await self.position_manager.check_orphans()

    # ── Emergency close (delegates to PositionManager) ────────────────

    async def emergency_close(self) -> Optional[LiveTrade]:
        trade_result = await self.position_manager.emergency_close()
        if trade_result is None:
            return None

        trade = self._build_trade(trade_result)
        self.sizer.record_trade(trade.pnl)
        self.trades.append(trade)
        return trade

    # ── Close all exchange positions ──────────────────────────────────

    async def close_all_exchange_positions(self) -> list[dict]:
        return await self.position_manager.close_all_exchange_positions()

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _build_trade(result: dict) -> LiveTrade:
        entry_time = result.get("entry_time", "")
        if isinstance(entry_time, str):
            try:
                entry_time = datetime.fromisoformat(entry_time)
            except (ValueError, TypeError):
                entry_time = datetime.now(timezone.utc)

        exit_time = result.get("exit_time", "")
        if isinstance(exit_time, str):
            try:
                exit_time = datetime.fromisoformat(exit_time)
            except (ValueError, TypeError):
                exit_time = datetime.now(timezone.utc)

        return LiveTrade(
            ticker=result["ticker"],
            direction=result["direction"],
            contracts=result["contracts"],
            entry_price=result["entry_price"],
            exit_price=result["exit_price"],
            pnl=result["pnl"],
            pnl_pct=result["pnl_pct"],
            fees=result["fees"],
            exit_reason=result["exit_reason"],
            conviction=result["conviction"],
            regime_at_entry=result["regime_at_entry"],
            candles_held=result["candles_held"],
            entry_time=entry_time,
            exit_time=exit_time,
            entry_order_id=result.get("entry_order_id"),
            exit_order_id=result.get("exit_order_id"),
            entry_obi=result.get("entry_obi", 0.0),
            entry_roc=result.get("entry_roc", 0.0),
            signal_driver=result.get("signal_driver", "-"),
            entry_cost_dollars=result.get("entry_cost_dollars"),
            exit_cost_dollars=result.get("exit_cost_dollars"),
            entry_fill_source=result.get("entry_fill_source", "order_response"),
            exit_fill_source=result.get("exit_fill_source", "order_response"),
            wallet_at_entry=result.get("wallet_at_entry"),
        )

    def get_state(self) -> dict:
        pm_state = self.position_manager.get_state()
        pm_state["total_trades"] = len(self.trades)
        pm_state["recent_trades"] = [
            {
                "ticker": t.ticker,
                "direction": t.direction,
                "pnl": t.pnl,
                "exit_reason": t.exit_reason,
                "exit_time": t.exit_time.isoformat(),
            }
            for t in self.trades[-10:]
        ]
        return pm_state
