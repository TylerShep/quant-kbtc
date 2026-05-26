"""
Contract-level time series helpers for contract-price backtesting.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass
class ContractTick:
    timestamp: float
    ticker: str
    mid_cents: Optional[float]
    best_bid: Optional[float]
    best_ask: Optional[float]
    spread_cents: Optional[float]
    obi: float = 0.5
    total_bid_vol: float = 0.0
    total_ask_vol: float = 0.0
    source: str = "ob_mid"


@dataclass
class ContractTimeline:
    ticker: str
    ticks: list[ContractTick] = field(default_factory=list)
    close_time: Optional[float] = None
    result: Optional[str] = None
    expiration_value: Optional[float] = None
    _timestamps: list[float] = field(default_factory=list, init=False, repr=False)
    _is_finalized: bool = field(default=False, init=False, repr=False)

    def add_tick(self, tick: ContractTick) -> None:
        self.ticks.append(tick)
        self._is_finalized = False

    def finalize(self) -> None:
        if self._is_finalized:
            return
        if not self.ticks:
            self._timestamps = []
            self._is_finalized = True
            return

        # Keep one tick per exact timestamp; prefer order-book derived mids.
        ordered = sorted(
            self.ticks,
            key=lambda t: (
                t.timestamp,
                0 if t.source == "ob_mid" else 1,
            ),
        )
        deduped: list[ContractTick] = []
        for tick in ordered:
            if not deduped or tick.timestamp != deduped[-1].timestamp:
                deduped.append(tick)
                continue
            prev = deduped[-1]
            if prev.source != "ob_mid" and tick.source == "ob_mid":
                deduped[-1] = tick
            elif prev.source == tick.source:
                deduped[-1] = tick

        self.ticks = deduped
        self._timestamps = [t.timestamp for t in deduped]
        self._is_finalized = True

    def iter_ticks(
        self,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
    ) -> Iterable[ContractTick]:
        self.finalize()
        if not self.ticks:
            return []
        lo = 0 if start_ts is None else bisect_left(self._timestamps, start_ts)
        hi = len(self.ticks) if end_ts is None else bisect_right(self._timestamps, end_ts)
        return self.ticks[lo:hi]

    def latest_tick_before(self, ts: float) -> Optional[ContractTick]:
        self.finalize()
        if not self.ticks:
            return None
        idx = bisect_right(self._timestamps, ts) - 1
        if idx < 0:
            return None
        return self.ticks[idx]

    def has_recent_ob_tick(self, ts: float, max_age_sec: float) -> bool:
        tick = self.latest_tick_before(ts)
        if tick is None or tick.source != "ob_mid":
            return False
        return (ts - tick.timestamp) <= max_age_sec
