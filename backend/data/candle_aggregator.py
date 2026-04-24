"""
CandleAggregator — builds 15-minute OHLCV candles from spot ticks.
Uses a deque ring buffer per the low-latency skill.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional


@dataclass
class Candle:
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    # Per-candle tick count. Used by ml.feature_capture as an "activity"
    # proxy when real per-tick spot volume isn't available (the spot WS
    # only exposes a rolling 24h cumulative figure, which can decrease
    # between ticks and so isn't a clean per-tick increment). Tick rate
    # is a standard market-microstructure proxy for trade-arrival
    # intensity. Always populated; defaults to 0 only on the first tick
    # of a brand-new candle (which immediately becomes 1 below).
    tick_count: int = 0


class CandleAggregator:
    """Aggregates spot ticks into 15-minute candles."""

    def __init__(self, interval_sec: int = 900, max_candles: int = 500):
        self.interval_sec = interval_sec
        self.candles: deque[Candle] = deque(maxlen=max_candles)
        self._current: Optional[Candle] = None
        self._current_boundary: int = 0

    def on_tick(self, timestamp: float, price: float, volume: float = 0.0) -> Optional[Candle]:
        """
        Feed a spot tick. Returns a completed Candle when a boundary is crossed.
        """
        boundary = int(timestamp) // self.interval_sec * self.interval_sec

        if self._current is None or boundary != self._current_boundary:
            completed = self._current
            self._current = Candle(
                timestamp=boundary,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=volume,
                tick_count=1,
            )
            self._current_boundary = boundary
            if completed is not None:
                self.candles.append(completed)
                return completed
            return None

        self._current.high = max(self._current.high, price)
        self._current.low = min(self._current.low, price)
        self._current.close = price
        self._current.volume += volume
        self._current.tick_count += 1
        return None

    @property
    def current(self) -> Optional[Candle]:
        return self._current

    def recent(self, n: int) -> list[Candle]:
        return list(self.candles)[-n:]
