"""
PaperTrader — simulated execution for paper trading mode.
Tracks positions, PnL, and trade history without touching the Kalshi API.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

import structlog

from risk.position_sizer import PositionSizer

logger = structlog.get_logger(__name__)


@dataclass
class PaperPosition:
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


@dataclass
class PaperTrade:
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


class PaperTrader:
    FEE_RATE = 0.007

    def __init__(self, sizer: PositionSizer):
        self.sizer = sizer
        self.position: Optional[PaperPosition] = None
        self.trades: List[PaperTrade] = []

    @property
    def has_position(self) -> bool:
        return self.position is not None

    def enter(
        self,
        ticker: str,
        direction: str,
        price: float,
        conviction: str,
        regime: str,
        obi: float = 0.0,
        roc: float = 0.0,
    ) -> Optional[PaperPosition]:
        if self.position is not None:
            return None

        size_dollars = self.sizer.calculate_size(conviction)
        cost_per = price / 100
        contracts = int(size_dollars / cost_per) if cost_per > 0 else 0
        if contracts < 1:
            logger.warning("paper.position_too_small", size=size_dollars, price=price)
            return None

        self.position = PaperPosition(
            ticker=ticker,
            direction=direction,
            contracts=contracts,
            entry_price=price,
            entry_time=datetime.now(timezone.utc),
            conviction=conviction,
            regime_at_entry=regime,
            entry_obi=obi,
            entry_roc=roc,
        )
        logger.info(
            "paper.entry",
            ticker=ticker,
            direction=direction,
            contracts=contracts,
            price=price,
            conviction=conviction,
        )
        return self.position

    def exit(self, price: float, reason: str) -> Optional[PaperTrade]:
        if self.position is None:
            return None

        pos = self.position
        d = 1 if pos.direction == "long" else -1
        pnl_per_contract = d * (price - pos.entry_price) / 100
        gross_pnl = pnl_per_contract * pos.contracts
        notional = pos.contracts * pos.entry_price / 100
        fees = notional * self.FEE_RATE
        net_pnl = gross_pnl - fees
        pnl_pct = net_pnl / notional if notional > 0 else 0

        trade = PaperTrade(
            ticker=pos.ticker,
            direction=pos.direction,
            contracts=pos.contracts,
            entry_price=pos.entry_price,
            exit_price=price,
            pnl=round(net_pnl, 4),
            pnl_pct=round(pnl_pct, 4),
            fees=round(fees, 4),
            exit_reason=reason,
            conviction=pos.conviction,
            regime_at_entry=pos.regime_at_entry,
            candles_held=pos.candles_held,
            entry_time=pos.entry_time,
            exit_time=datetime.now(timezone.utc),
        )

        self.sizer.record_trade(net_pnl)
        self.trades.append(trade)
        self.position = None

        logger.info(
            "paper.exit",
            ticker=trade.ticker,
            direction=trade.direction,
            pnl=trade.pnl,
            reason=reason,
        )
        return trade

    def handle_settlement(self, result: str) -> Optional[PaperTrade]:
        """Handle contract settlement by recording PnL at settled price."""
        if self.position is None:
            return None
        settled_price = 100 if result == "yes" else 0
        return self.exit(settled_price, "CONTRACT_SETTLED")

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
