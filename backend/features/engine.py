"""
FeatureEngine — computes OBI, ROC, ATR, and other features from market state.
Single update method called on every tick.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Dict, Optional

from config import settings


@dataclass
class FeatureSnapshot:
    obi: float
    total_bid_vol: float
    total_ask_vol: float
    spread_cents: Optional[int]
    spot_price: Optional[float]
    mid_price: Optional[float]

    def to_dict(self) -> dict:
        return {
            "obi": round(self.obi, 4),
            "total_bid_vol": self.total_bid_vol,
            "total_ask_vol": self.total_ask_vol,
            "spread_cents": self.spread_cents,
            "spot_price": self.spot_price,
            "mid_price": self.mid_price,
        }


class FeatureEngine:
    """Computes features from MarketState on each tick."""

    def __init__(self):
        self._obi_history: Dict[str, deque] = {}
        self._last_spot: Dict[str, float] = {}

    def update(self, symbol: str, state) -> Optional[FeatureSnapshot]:
        book = state.order_book
        depth = settings.obi.depth_levels

        bid_vol = sum(s for _, s in book.top_n_bids(depth))
        ask_vol = sum(s for _, s in book.top_n_asks(depth))
        total = bid_vol + ask_vol

        if total == 0:
            return None

        obi = bid_vol / total

        if symbol not in self._obi_history:
            self._obi_history[symbol] = deque(maxlen=20)
        self._obi_history[symbol].append(obi)

        spot = state.spot_price or self._last_spot.get(symbol)
        if state.spot_price:
            self._last_spot[symbol] = state.spot_price

        spread = book.spread
        spread_cents = int(spread) if spread is not None else None

        return FeatureSnapshot(
            obi=obi,
            total_bid_vol=bid_vol,
            total_ask_vol=ask_vol,
            spread_cents=spread_cents,
            spot_price=spot,
            mid_price=book.mid,
        )

    def obi_history(self, symbol: str) -> list[float]:
        return list(self._obi_history.get(symbol, []))
