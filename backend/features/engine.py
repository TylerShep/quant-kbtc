"""
FeatureEngine — computes OBI, ROC, ATR, and other features from market state.
Single update method called on every tick.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, Optional

from config import settings


@dataclass
class FeatureSnapshot:
    obi: float
    obi_raw: float
    total_bid_vol: float
    total_ask_vol: float
    spread_cents: Optional[int]
    spot_price: Optional[float]
    mid_price: Optional[float]

    def to_dict(self) -> dict:
        return {
            "obi": round(self.obi, 4),
            "obi_raw": round(self.obi_raw, 4),
            "total_bid_vol": self.total_bid_vol,
            "total_ask_vol": self.total_ask_vol,
            "spread_cents": self.spread_cents,
            "spot_price": self.spot_price,
            "mid_price": self.mid_price,
        }


class OBISmoother:
    """Time-windowed median smoother with adaptive window sizing.

    Maintains a timestamped buffer of raw OBI values. The smoothed value
    is the median over the last ``base_window_sec`` seconds. When the
    book is noisy (high OBI stdev) the window expands; when stable it
    contracts — so the signal responds quickly in calm markets but
    resists whipsaw in noisy ones.
    """

    STDEV_LOOKBACK_SEC = 60.0
    NOISY_THRESHOLD = 0.15
    STABLE_THRESHOLD = 0.05
    EXPAND_MULT = 1.5
    CONTRACT_MULT = 0.75

    def __init__(self, base_window_sec: float = 5.0, min_samples: int = 3):
        self._base_window = base_window_sec
        self._min_samples = min_samples
        self._buffer: deque[tuple[float, float]] = deque(maxlen=2000)
        self._stdev_buffer: deque[tuple[float, float]] = deque(maxlen=2000)

    def update(self, obi: float) -> float:
        now = time.time()
        self._buffer.append((now, obi))
        self._stdev_buffer.append((now, obi))

        stdev_cutoff = now - self.STDEV_LOOKBACK_SEC
        while self._stdev_buffer and self._stdev_buffer[0][0] < stdev_cutoff:
            self._stdev_buffer.popleft()

        window = self._base_window
        if len(self._stdev_buffer) >= 5:
            vals = [v for _, v in self._stdev_buffer]
            mean = sum(vals) / len(vals)
            variance = sum((v - mean) ** 2 for v in vals) / len(vals)
            stdev = math.sqrt(variance)
            if stdev > self.NOISY_THRESHOLD:
                window = self._base_window * self.EXPAND_MULT
            elif stdev < self.STABLE_THRESHOLD:
                window = self._base_window * self.CONTRACT_MULT

        cutoff = now - window
        while self._buffer and self._buffer[0][0] < cutoff:
            self._buffer.popleft()

        vals_in_window = [v for _, v in self._buffer]
        if len(vals_in_window) < self._min_samples:
            return obi

        vals_in_window.sort()
        mid = len(vals_in_window) // 2
        if len(vals_in_window) % 2 == 0:
            return (vals_in_window[mid - 1] + vals_in_window[mid]) / 2
        return vals_in_window[mid]


class FeatureEngine:
    """Computes features from MarketState on each tick."""

    def __init__(self):
        self._obi_history: Dict[str, deque] = {}
        self._obi_smoothers: Dict[str, OBISmoother] = {}
        self._last_spot: Dict[str, float] = {}

    def update(self, symbol: str, state) -> Optional[FeatureSnapshot]:
        book = state.order_book
        depth = settings.obi.depth_levels

        bid_vol = sum(s for _, s in book.top_n_bids(depth))
        ask_vol = sum(s for _, s in book.top_n_asks(depth))
        total = bid_vol + ask_vol

        if total == 0:
            return None

        obi_raw = bid_vol / total

        if symbol not in self._obi_smoothers:
            self._obi_smoothers[symbol] = OBISmoother(
                base_window_sec=settings.obi.smooth_window_sec,
                min_samples=settings.obi.smooth_min_samples,
            )
        obi_smoothed = self._obi_smoothers[symbol].update(obi_raw)

        if symbol not in self._obi_history:
            self._obi_history[symbol] = deque(maxlen=20)
        self._obi_history[symbol].append(obi_smoothed)

        spot = state.spot_price or self._last_spot.get(symbol)
        if state.spot_price:
            self._last_spot[symbol] = state.spot_price

        spread = book.spread
        spread_cents = int(spread) if spread is not None else None

        return FeatureSnapshot(
            obi=obi_smoothed,
            obi_raw=obi_raw,
            total_bid_vol=bid_vol,
            total_ask_vol=ask_vol,
            spread_cents=spread_cents,
            spot_price=spot,
            mid_price=book.mid,
        )

    def obi_history(self, symbol: str) -> list[float]:
        return list(self._obi_history.get(symbol, []))
