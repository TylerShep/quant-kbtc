"""
ATR Volatility Regime Filter — gates all strategy entries.
Per the atr-regime-filter skill.
"""
from __future__ import annotations

from collections import deque
from typing import Optional

from config import settings

REGIME_STRATEGY_MAP = {
    "LOW": {"obi": True, "roc": False},
    "MEDIUM": {"obi": True, "roc": True},
    "HIGH": {"obi": False, "roc": False},
}


class ATRRegimeFilter:
    def __init__(self):
        cfg = settings.atr
        self.tr_history: deque[float] = deque(maxlen=cfg.period)
        self.atr_pct_history: deque[float] = deque(maxlen=cfg.smooth_period)
        self.regime_history: deque[str] = deque(maxlen=cfg.regime_confirm_bars)
        self.prev_close: Optional[float] = None
        self.current_regime: str = "MEDIUM"

    def update(self, high: float, low: float, close: float) -> str:
        cfg = settings.atr

        if self.prev_close is None:
            self.prev_close = close
            return self.current_regime

        tr = max(
            high - low,
            abs(high - self.prev_close),
            abs(low - self.prev_close),
        )
        self.tr_history.append(tr)
        self.prev_close = close

        if len(self.tr_history) < cfg.period:
            return self.current_regime

        atr = sum(self.tr_history) / len(self.tr_history)
        atr_pct = (atr / close) * 100 if close > 0 else 0
        self.atr_pct_history.append(atr_pct)

        smoothed = sum(self.atr_pct_history) / len(self.atr_pct_history)
        raw_regime = self._classify(smoothed)
        self.regime_history.append(raw_regime)

        if all(r == raw_regime for r in self.regime_history):
            self.current_regime = raw_regime

        return self.current_regime

    def _classify(self, atr_pct: float) -> str:
        cfg = settings.atr
        if atr_pct < cfg.low_threshold:
            return "LOW"
        if atr_pct > cfg.high_threshold:
            return "HIGH"
        return "MEDIUM"

    def strategy_allowed(self, strategy_name: str) -> bool:
        return REGIME_STRATEGY_MAP[self.current_regime].get(strategy_name, False)

    def warmup(self, candles: list[tuple[float, float, float]]) -> int:
        """Pre-seed from historical (high, low, close) tuples.

        Call once at startup before the tick loop begins so that
        atr_pct_history and regime are populated from minute one.
        Returns the number of candles consumed.
        """
        consumed = 0
        for high, low, close in candles:
            self.update(high, low, close)
            consumed += 1
        return consumed

    def get_state(self) -> dict:
        return {
            "regime": self.current_regime,
            "atr_pct": round(self.atr_pct_history[-1], 4) if self.atr_pct_history else None,
            "smoothed": round(
                sum(self.atr_pct_history) / len(self.atr_pct_history), 4
            )
            if self.atr_pct_history
            else None,
        }
