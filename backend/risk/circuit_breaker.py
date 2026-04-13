"""
Circuit breaker — daily/weekly/max drawdown halt logic.
Per the risk-position-sizing skill.
"""
from __future__ import annotations

from typing import Optional, Tuple

from config import settings
from risk.position_sizer import PositionSizer


class CircuitBreaker:
    def __init__(self, sizer: PositionSizer, never_halt: bool = False):
        self.sizer = sizer
        self.never_halt = never_halt

    def can_trade(self) -> Tuple[bool, Optional[str]]:
        if self.never_halt:
            return True, None

        cfg = settings.risk

        if self.sizer.daily_loss >= cfg.daily_loss_limit_pct:
            return False, "DAILY_LOSS_LIMIT"

        if self.sizer.weekly_loss >= cfg.weekly_loss_limit_pct:
            return False, "WEEKLY_LOSS_LIMIT"

        if self.sizer.current_drawdown >= cfg.max_drawdown_pct:
            return False, "MAX_DRAWDOWN_HALT"

        return True, None

    def get_state(self) -> dict:
        can, reason = self.can_trade()
        return {
            "can_trade": can,
            "halt_reason": reason,
            **self.sizer.get_state(),
        }
