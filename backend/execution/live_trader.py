"""
LiveTrader — real execution against the Kalshi API.
Mirrors PaperTrader interface but places actual orders.
Only activate after backtesting validates the strategy.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import structlog

from data.kalshi_ws import KalshiOrderClient
from risk.position_sizer import PositionSizer

logger = structlog.get_logger(__name__)


@dataclass
class LivePosition:
    ticker: str
    direction: str
    contracts: int
    entry_price: float
    entry_time: datetime
    conviction: str
    regime_at_entry: str
    entry_obi: float = 0.0
    entry_roc: float = 0.0
    candles_held: int = 0
    order_id: Optional[str] = None


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


class LiveTrader:
    FEE_RATE = 0.007

    def __init__(self, sizer: PositionSizer):
        self.sizer = sizer
        self.client = KalshiOrderClient()
        self.position: Optional[LivePosition] = None
        self.trades: list[LiveTrade] = []

    @property
    def has_position(self) -> bool:
        return self.position is not None

    async def enter(
        self,
        ticker: str,
        direction: str,
        price: float,
        conviction: str,
        regime: str,
        obi: float = 0.0,
        roc: float = 0.0,
    ) -> Optional[LivePosition]:
        if self.position is not None:
            return None

        size_dollars = self.sizer.calculate_size(conviction)
        cost_per = price / 100
        contracts = int(size_dollars / cost_per) if cost_per > 0 else 0
        if contracts < 1:
            logger.warning("live.position_too_small", size=size_dollars, price=price)
            return None

        side = "yes" if direction == "long" else "no"
        yes_price = int(price) if direction == "long" else None
        no_price = int(100 - price) if direction == "short" else None

        try:
            result = await self.client.create_order(
                ticker=ticker,
                side=side,
                action="buy",
                count=contracts,
                type="market",
                yes_price=yes_price,
                no_price=no_price,
            )
            order_id = result.get("order", {}).get("order_id")
            logger.info("live.order_placed", ticker=ticker, direction=direction,
                        contracts=contracts, order_id=order_id)
        except Exception as e:
            logger.error("live.order_failed", error=str(e), ticker=ticker)
            return None

        fill_price = price
        if order_id:
            try:
                await asyncio.sleep(1)
                order_detail = await self.client.get_order(order_id)
                order_data = order_detail.get("order", {})
                if order_data.get("status") == "filled":
                    fill_price = order_data.get("yes_price", price)
            except Exception:
                pass

        self.position = LivePosition(
            ticker=ticker,
            direction=direction,
            contracts=contracts,
            entry_price=fill_price,
            entry_time=datetime.now(timezone.utc),
            conviction=conviction,
            regime_at_entry=regime,
            entry_obi=obi,
            entry_roc=roc,
            order_id=order_id,
        )
        logger.info("live.entry", ticker=ticker, direction=direction,
                     contracts=contracts, price=fill_price, conviction=conviction)
        return self.position

    async def exit(self, price: float, reason: str) -> Optional[LiveTrade]:
        if self.position is None:
            return None

        pos = self.position
        side = "no" if pos.direction == "long" else "yes"

        exit_order_id = None
        try:
            result = await self.client.create_order(
                ticker=pos.ticker,
                side=side,
                action="buy",
                count=pos.contracts,
                type="market",
            )
            exit_order_id = result.get("order", {}).get("order_id")
            logger.info("live.exit_order_placed", ticker=pos.ticker, order_id=exit_order_id)
        except Exception as e:
            logger.error("live.exit_order_failed", error=str(e), ticker=pos.ticker)
            return None

        exit_price = price
        if exit_order_id:
            try:
                await asyncio.sleep(1)
                order_detail = await self.client.get_order(exit_order_id)
                order_data = order_detail.get("order", {})
                if order_data.get("status") == "filled":
                    exit_price = order_data.get("yes_price", price)
            except Exception:
                pass

        d = 1 if pos.direction == "long" else -1
        pnl_per_contract = d * (exit_price - pos.entry_price) / 100
        gross_pnl = pnl_per_contract * pos.contracts
        notional = pos.contracts * pos.entry_price / 100
        fees = notional * self.FEE_RATE
        net_pnl = gross_pnl - fees
        pnl_pct = net_pnl / notional if notional > 0 else 0

        trade = LiveTrade(
            ticker=pos.ticker,
            direction=pos.direction,
            contracts=pos.contracts,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            pnl=round(net_pnl, 4),
            pnl_pct=round(pnl_pct, 4),
            fees=round(fees, 4),
            exit_reason=reason,
            conviction=pos.conviction,
            regime_at_entry=pos.regime_at_entry,
            candles_held=pos.candles_held,
            entry_time=pos.entry_time,
            exit_time=datetime.now(timezone.utc),
            entry_order_id=pos.order_id,
            exit_order_id=exit_order_id,
        )

        self.sizer.record_trade(net_pnl)
        self.trades.append(trade)
        self.position = None

        logger.info("live.exit", ticker=trade.ticker, direction=trade.direction,
                     pnl=trade.pnl, reason=reason)
        return trade

    def get_state(self) -> dict:
        return {
            "has_position": self.has_position,
            "position": {
                "ticker": self.position.ticker,
                "direction": self.position.direction,
                "contracts": self.position.contracts,
                "entry_price": self.position.entry_price,
                "candles_held": self.position.candles_held,
                "conviction": self.position.conviction,
            }
            if self.position
            else None,
            "total_trades": len(self.trades),
            "recent_trades": [
                {
                    "ticker": t.ticker,
                    "direction": t.direction,
                    "pnl": t.pnl,
                    "exit_reason": t.exit_reason,
                    "exit_time": t.exit_time.isoformat(),
                }
                for t in self.trades[-10:]
            ],
        }
