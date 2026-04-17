"""
Trend-aware short guard.

Mitigates persistent short bias by reducing or blocking short entries during
recent uptrends, without changing core signal generation.
"""
from __future__ import annotations

from typing import Optional, Sequence

import structlog

from config import settings
from strategies.obi import Direction
from strategies.resolver import Conviction, TradeDecision

logger = structlog.get_logger()


class TrendGuard:
    """Adjust short entries based on recent close-to-close trend."""

    def __init__(
        self,
        lookback_candles: Optional[int] = None,
        soften_rise_pct: Optional[float] = None,
        block_rise_pct: Optional[float] = None,
    ) -> None:
        self.lookback_candles = (
            lookback_candles
            if lookback_candles is not None
            else settings.risk.short_trend_lookback_candles
        )
        self.soften_rise_pct = (
            soften_rise_pct
            if soften_rise_pct is not None
            else settings.risk.short_trend_soften_rise_pct
        )
        self.block_rise_pct = (
            block_rise_pct
            if block_rise_pct is not None
            else settings.risk.short_trend_block_rise_pct
        )

    def apply_short_trend_filter(
        self,
        decision: TradeDecision,
        closes: Sequence[float],
        mode: str,
    ) -> Optional[str]:
        """Mutates decision in-place. Returns block reason when entry is blocked."""
        if decision.direction != Direction.SHORT or not decision.should_trade:
            return None
        if self.lookback_candles < 2 or len(closes) < self.lookback_candles:
            return None

        start = closes[-self.lookback_candles]
        end = closes[-1]
        if start <= 0:
            return None

        rise_pct = ((end - start) / start) * 100.0
        if rise_pct >= self.block_rise_pct:
            reason = (
                f"SHORT_BLOCKED_UPTREND_{rise_pct:.3f}%>="
                f"{self.block_rise_pct:.3f}%"
            )
            decision.direction = None
            decision.conviction = Conviction.NONE
            decision.skip_reason = reason
            logger.info(
                "trend_guard.short_blocked",
                mode=mode,
                rise_pct=round(rise_pct, 4),
                lookback=self.lookback_candles,
                threshold=self.block_rise_pct,
            )
            return reason

        if rise_pct >= self.soften_rise_pct:
            old_conviction = decision.conviction
            if old_conviction == Conviction.HIGH:
                decision.conviction = Conviction.NORMAL
            elif old_conviction == Conviction.NORMAL:
                decision.conviction = Conviction.LOW
            else:
                reason = (
                    f"SHORT_BLOCKED_UPTREND_LOW_{rise_pct:.3f}%>="
                    f"{self.soften_rise_pct:.3f}%"
                )
                decision.direction = None
                decision.conviction = Conviction.NONE
                decision.skip_reason = reason
                logger.info(
                    "trend_guard.short_blocked_low_conviction",
                    mode=mode,
                    rise_pct=round(rise_pct, 4),
                    lookback=self.lookback_candles,
                    threshold=self.soften_rise_pct,
                )
                return reason

            logger.info(
                "trend_guard.short_conviction_downgraded",
                mode=mode,
                rise_pct=round(rise_pct, 4),
                lookback=self.lookback_candles,
                from_conviction=old_conviction.value,
                to_conviction=decision.conviction.value,
            )

        return None
