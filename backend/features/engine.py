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
    spot_roc_30s: Optional[float] = None
    spot_roc_60s: Optional[float] = None
    spot_momentum_decay: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "obi": round(self.obi, 4),
            "obi_raw": round(self.obi_raw, 4),
            "total_bid_vol": self.total_bid_vol,
            "total_ask_vol": self.total_ask_vol,
            "spread_cents": self.spread_cents,
            "spot_price": self.spot_price,
            "mid_price": self.mid_price,
            "spot_roc_30s": (
                round(self.spot_roc_30s, 6)
                if self.spot_roc_30s is not None else None
            ),
            "spot_roc_60s": (
                round(self.spot_roc_60s, 6)
                if self.spot_roc_60s is not None else None
            ),
            "spot_momentum_decay": (
                round(self.spot_momentum_decay, 6)
                if self.spot_momentum_decay is not None else None
            ),
        }


class OBISmoother:
    """Time-windowed median smoother with adaptive window sizing.

    Maintains a timestamped buffer of raw OBI values. The smoothed value
    is the median over the last ``base_window_sec`` seconds. When the
    book is noisy (high OBI stdev) the window expands; when stable it
    contracts — so the signal responds quickly in calm markets but
    resists whipsaw in noisy ones.

    2026-05-06 (BUG-035 follow-up #2): the original implementation
    rebuilt the mean and variance over the full 60s stdev buffer on
    every tick (two O(n) passes via list comp + generator), eating
    ~55% of MainThread CPU at the 20+ Hz market-tick rate. py-spy
    consistently pinned line 88 (variance generator) as the dominant
    hot frame, which was starving the Coinbase WS keepalive.

    The fix: keep running ``sum`` and ``sum_of_squares`` so each
    update is O(1) — appends add to the sums, evictions subtract.
    Variance is then ``E[X^2] - E[X]^2``. The numerical drift from
    accumulated float error is bounded by the 60s/2000-entry window
    (the buffer naturally rolls over fast enough that errors don't
    compound), and the comparison thresholds (0.15, 0.05) are coarse
    enough that the last-ULP differences don't change which branch
    we take. We periodically reseed the running sums from the buffer
    contents to keep drift bounded if the loop ever stalls long
    enough for a single value to dominate the accumulator history.
    """

    STDEV_LOOKBACK_SEC = 60.0
    NOISY_THRESHOLD = 0.15
    STABLE_THRESHOLD = 0.05
    EXPAND_MULT = 1.5
    CONTRACT_MULT = 0.75
    # Reseed running sums from the buffer every N evictions to bound
    # accumulated float error. Cheap (one O(n) pass per ~1000 ticks
    # vs one O(n) pass per tick before).
    _RESEED_EVERY = 1000

    def __init__(self, base_window_sec: float = 5.0, min_samples: int = 3):
        self._base_window = base_window_sec
        self._min_samples = min_samples
        self._buffer: deque[tuple[float, float]] = deque(maxlen=2000)
        self._stdev_buffer: deque[tuple[float, float]] = deque(maxlen=2000)
        # Incremental moments over ``_stdev_buffer`` so variance is O(1).
        self._stdev_sum: float = 0.0
        self._stdev_sum_sq: float = 0.0
        self._evictions_since_reseed: int = 0

    def _reseed_stdev_moments(self) -> None:
        """Recompute running sums from buffer contents to bound float drift."""
        s = 0.0
        ss = 0.0
        for _, v in self._stdev_buffer:
            s += v
            ss += v * v
        self._stdev_sum = s
        self._stdev_sum_sq = ss
        self._evictions_since_reseed = 0

    def update(self, obi: float) -> float:
        now = time.time()
        self._buffer.append((now, obi))

        # Append to stdev buffer + maintain running moments incrementally.
        # If maxlen evicts an element implicitly we still need to subtract
        # it from the running sums; capture the would-be-evicted value
        # before append.
        evicted_val: Optional[float] = None
        if len(self._stdev_buffer) == self._stdev_buffer.maxlen:
            evicted_val = self._stdev_buffer[0][1]
        self._stdev_buffer.append((now, obi))
        if evicted_val is not None:
            self._stdev_sum -= evicted_val
            self._stdev_sum_sq -= evicted_val * evicted_val
            self._evictions_since_reseed += 1
        self._stdev_sum += obi
        self._stdev_sum_sq += obi * obi

        # Evict everything outside the 60s lookback and adjust moments.
        stdev_cutoff = now - self.STDEV_LOOKBACK_SEC
        while self._stdev_buffer and self._stdev_buffer[0][0] < stdev_cutoff:
            _, v = self._stdev_buffer.popleft()
            self._stdev_sum -= v
            self._stdev_sum_sq -= v * v
            self._evictions_since_reseed += 1

        if self._evictions_since_reseed >= self._RESEED_EVERY:
            self._reseed_stdev_moments()

        window = self._base_window
        n = len(self._stdev_buffer)
        if n >= 5:
            mean = self._stdev_sum / n
            # E[X^2] - E[X]^2; clamp to 0 to absorb float underflow.
            variance = max(0.0, self._stdev_sum_sq / n - mean * mean)
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

    _MOMENTUM_HISTORY_SEC = 180.0
    _MOMENTUM_FAST_SEC = 30.0
    _MOMENTUM_SLOW_SEC = 60.0

    def __init__(self):
        self._obi_history: Dict[str, deque] = {}
        self._obi_smoothers: Dict[str, OBISmoother] = {}
        self._last_spot: Dict[str, float] = {}
        self._spot_history: Dict[str, deque[tuple[float, float]]] = {}

    @staticmethod
    def _price_n_seconds_ago(
        history: deque[tuple[float, float]],
        now_ts: float,
        seconds: float,
    ) -> Optional[float]:
        cutoff = now_ts - seconds
        candidate = None
        for ts, price in history:
            if ts <= cutoff:
                candidate = price
            else:
                break
        if candidate is not None:
            return candidate
        # If the stream hasn't been running for the full window yet,
        # fall back to earliest seen price as a warmup estimate.
        if history:
            return history[0][1]
        return None

    @staticmethod
    def _roc_pct(current: Optional[float], past: Optional[float]) -> Optional[float]:
        if current is None or past is None or past == 0:
            return None
        return ((current - past) / past) * 100.0

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

        spot_roc_30s: Optional[float] = None
        spot_roc_60s: Optional[float] = None
        spot_momentum_decay: Optional[float] = None
        if spot is not None:
            now_ts = time.time()
            if symbol not in self._spot_history:
                self._spot_history[symbol] = deque(maxlen=4096)
            history = self._spot_history[symbol]
            history.append((now_ts, spot))
            cutoff = now_ts - self._MOMENTUM_HISTORY_SEC
            while history and history[0][0] < cutoff:
                history.popleft()

            price_30s = self._price_n_seconds_ago(
                history, now_ts, self._MOMENTUM_FAST_SEC
            )
            price_60s = self._price_n_seconds_ago(
                history, now_ts, self._MOMENTUM_SLOW_SEC
            )
            spot_roc_30s = self._roc_pct(spot, price_30s)
            spot_roc_60s = self._roc_pct(spot, price_60s)
            if (
                spot_roc_30s is not None
                and spot_roc_60s is not None
                and abs(spot_roc_60s) > 1e-9
            ):
                spot_momentum_decay = spot_roc_30s / spot_roc_60s

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
            spot_roc_30s=spot_roc_30s,
            spot_roc_60s=spot_roc_60s,
            spot_momentum_decay=spot_momentum_decay,
        )

    def obi_history(self, symbol: str) -> list[float]:
        return list(self._obi_history.get(symbol, []))
