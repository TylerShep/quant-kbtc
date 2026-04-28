"""
Dynamic entry price guards — prevents trades at extreme prices
where binary settlement risk dominates.

Direction-asymmetric bounds: shorts are blocked below SHORT_MIN_ENTRY_PRICE
(default 25c), longs are blocked above LONG_MAX_ENTRY_PRICE (default 60c).
Bounds also adapt to ATR regime and time remaining in the contract.
"""
from __future__ import annotations

from typing import Optional, Tuple

import structlog

from config import settings

logger = structlog.get_logger()

LONG_BOUNDS: dict[str, dict] = {
    "LOW":    {"min_price": 20, "max_price": 60},
    "MEDIUM": {"min_price": 15, "max_price": 60},
    "HIGH":   {"min_price": 100, "max_price": 0},
}

SHORT_BOUNDS: dict[str, dict] = {
    "LOW":    {"min_price": 25, "max_price": 80},
    "MEDIUM": {"min_price": 25, "max_price": 85},
    "HIGH":   {"min_price": 100, "max_price": 0},
}


class PriceGuard:
    """Gate entries based on price, direction, regime, and time remaining."""

    def is_allowed(
        self,
        entry_price: float,
        direction: str,
        atr_regime: str,
        time_remaining_sec: Optional[int],
    ) -> Tuple[bool, Optional[str]]:
        """Returns (allowed, rejection_reason)."""
        if atr_regime == "HIGH":
            return False, "REGIME_HIGH"

        if time_remaining_sec is not None and time_remaining_sec < 180:
            return False, "EXPIRY_TOO_CLOSE"

        cfg = settings.risk

        # Short-specific late-cycle block. Paper attribution on 21d of trades
        # showed shorts entered with <13 min to close had 0-30% WR and lost
        # $6.6k net across 27 trades, while shorts entered with >=13 min were
        # 59% WR / +$1k net. The settlement-time gamma on Kalshi 15-min
        # contracts blows up underwater shorts long before SHORT_SETTLEMENT_GUARD
        # at the exit layer can save them. Block on entry instead.
        if (direction == "short"
                and time_remaining_sec is not None
                and time_remaining_sec < cfg.short_min_seconds_to_expiry):
            return False, (
                f"SHORT_ENTRY_TOO_CLOSE_TO_EXPIRY_{time_remaining_sec}s"
                f"<{cfg.short_min_seconds_to_expiry}s"
            )

        if direction == "long":
            bounds = LONG_BOUNDS.get(atr_regime, LONG_BOUNDS["MEDIUM"])
            min_p = bounds["min_price"]
            max_p = min(bounds["max_price"], cfg.long_max_entry_price)

            if time_remaining_sec is not None and time_remaining_sec < 300:
                min_p = max(min_p - 5, 5)
                max_p = min(max_p + 5, 95)

            if entry_price < min_p:
                return False, f"YES_PRICE_TOO_LOW_{entry_price}c<{min_p}c"
            if entry_price > max_p:
                return False, f"YES_PRICE_TOO_HIGH_{entry_price}c>{max_p}c"
        else:
            bounds = SHORT_BOUNDS.get(atr_regime, SHORT_BOUNDS["MEDIUM"])
            short_floor = max(bounds["min_price"], cfg.short_min_entry_price)
            max_p = bounds["max_price"]

            # NOTE: previously this branch widened short max_p by 5c when
            # ``time_remaining_sec < 300``, i.e. became MORE permissive
            # exactly inside the gamma blow-up window. Removed 2026-04-28
            # because the new ``short_min_seconds_to_expiry`` guard above
            # now hard-blocks the same window, and even outside the block
            # there's no empirical reason to loosen the upper price bound
            # late in a contract's life -- if anything the opposite.

            if entry_price < short_floor:
                return False, f"SHORT_ENTRY_TOO_CHEAP_{entry_price}c<{short_floor}c"
            no_price = 100 - entry_price
            if no_price > max_p:
                return False, f"NO_PRICE_TOO_HIGH_{no_price}c>{max_p}c"

        return True, None
