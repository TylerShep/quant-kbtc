"""
SpreadRegimeFilter — mirrors the ATRRegimeFilter pattern.

Maintains a rolling deque of spread_cents readings from each tick's
FeatureSnapshot. Provides spread_history() for evaluate_spread_divergence
and get_state() for the dashboard and signal log.

Staleness detection: if current_spread hasn't been updated within
SD_STALENESS_SEC, spread_history returns an empty list so that
evaluate_spread_divergence returns NORMAL (safe default).
"""
from __future__ import annotations

import time
from collections import deque
from typing import Optional

from config import settings


class SpreadRegimeFilter:
    def __init__(self):
        cfg = settings.spread_div
        self._history: deque[float] = deque(maxlen=cfg.baseline_window * 3)
        self._last_update: float = 0.0

    def update(self, spread_cents: Optional[float]) -> None:
        """Called every tick when a new FeatureSnapshot is produced."""
        if spread_cents is None or spread_cents <= 0:
            return
        self._history.append(float(spread_cents))
        self._last_update = time.time()

    def spread_history(self) -> list[float]:
        """Return the spread history list.

        Returns empty list if data is stale (no update within staleness window),
        which causes evaluate_spread_divergence to return NORMAL safely.
        """
        cfg = settings.spread_div
        if self._last_update == 0:
            return []
        if time.time() - self._last_update > cfg.staleness_sec:
            return []
        return list(self._history)

    def warmup(self, spread_values: list[float]) -> int:
        """Pre-seed from historical spread_cents values at startup.

        Call once after DB load, before the tick loop begins.
        Returns number of values consumed.
        """
        consumed = 0
        for v in spread_values:
            if v is not None and v > 0:
                self._history.append(float(v))
                consumed += 1
        if self._history:
            self._last_update = time.time()
        return consumed

    def get_state(self) -> dict:
        history = self.spread_history()
        if not history:
            return {"spread_state": "UNKNOWN", "baseline_cents": None, "history_len": 0}
        from strategies.spread_div import _median
        cfg = settings.spread_div
        baseline = _median(history[-cfg.baseline_window:])
        return {
            "baseline_cents": round(baseline, 2),
            "history_len": len(history),
            "last_update_age_sec": round(time.time() - self._last_update, 1),
        }
