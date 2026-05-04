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
from risk.fee_engine import FeeEngine

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
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    signal_driver: str = "-"


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
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    signal_driver: str = "-"
    # BUG-030 (2026-05-02): entry_obi and entry_roc were missing from
    # PaperTrade entirely, so coordinator's `getattr(trade, "entry_obi",
    # 0.0)` always defaulted to 0. The trades.entry_obi / entry_roc
    # columns were 100% NULL/zero across all 532 paper trades, breaking
    # any attribution (incl. the new edge_profile_review's ROC bucketing)
    # that joins on the trades table. Live trades stored the values
    # correctly; only paper was broken. trade_features.roc_5 remains the
    # source of truth for ML, but the trades table is the canonical row
    # for human/dashboard lookups.
    entry_obi: float = 0.0
    entry_roc: float = 0.0


class PaperTrader:
    FEE_RATE = 0.007

    def __init__(self, sizer: PositionSizer):
        self.sizer = sizer
        self.position: Optional[PaperPosition] = None
        self.trades: List[PaperTrade] = []
        self._fee_engine = FeeEngine()

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
        signal_driver: str = "-",
    ) -> Optional[PaperPosition]:
        if self.position is not None:
            return None

        size_dollars = self.sizer.calculate_size(conviction, direction)
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
            signal_driver=signal_driver,
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
        fees = self._fee_engine.compute_round_trip_fee(
            contracts=pos.contracts,
            entry_price_cents=pos.entry_price,
            exit_price_cents=price,
            entry_type="taker",
            exit_type="taker",
        )
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
            max_favorable_excursion=pos.max_favorable_excursion,
            max_adverse_excursion=pos.max_adverse_excursion,
            signal_driver=pos.signal_driver,
            entry_obi=pos.entry_obi,
            entry_roc=pos.entry_roc,
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
        # BUG-022 fix: refuse to settle with an ambiguous result. Old code
        # silently treated any non-"yes" value as a loss, which produced
        # phantom losses whenever settlement fired before finalization.
        if result not in ("yes", "no"):
            logger.warning(
                "paper.settlement_invalid_result",
                ticker=self.position.ticker,
                result_raw=result,
            )
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
                "signal_driver": self.position.signal_driver,
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
