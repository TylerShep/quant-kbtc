"""
OBI (Order Book Imbalance) strategy — primary signal.
Per the obi-trading skill.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from config import settings


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


def evaluate_obi(
    obi_history: list[float],
    total_book_volume: float,
    atr_regime: str,
    has_position: bool,
) -> Direction:
    """
    Evaluate OBI signal direction.
    Returns LONG, SHORT, or NEUTRAL.
    """
    cfg = settings.obi

    if has_position:
        return Direction.NEUTRAL

    if atr_regime == "HIGH":
        return Direction.NEUTRAL

    if total_book_volume < cfg.min_book_volume:
        return Direction.NEUTRAL

    n = cfg.consecutive_readings
    if len(obi_history) < n:
        return Direction.NEUTRAL

    recent = obi_history[-n:]

    if all(r >= cfg.long_threshold for r in recent):
        return Direction.LONG

    if all(r <= cfg.short_threshold for r in recent):
        return Direction.SHORT

    return Direction.NEUTRAL


def check_obi_exit(
    direction: str,
    current_obi: float,
    pnl_pct: float,
    candles_held: int,
    atr_regime: str,
) -> Optional[str]:
    """Check OBI-specific exit conditions. Returns exit reason or None."""
    cfg = settings.obi
    risk = settings.risk

    if pnl_pct <= -risk.stop_loss_pct:
        return "STOP_LOSS"

    if pnl_pct >= risk.stop_loss_pct * risk.profit_target_mult:
        return "TAKE_PROFIT"

    if direction == "long" and current_obi < cfg.neutral_exit_long:
        return "SIGNAL_DECAY"

    if direction == "short" and current_obi > cfg.neutral_exit_short:
        return "SIGNAL_DECAY"

    if atr_regime == "HIGH":
        return "VOLATILITY_SPIKE"

    if candles_held >= cfg.max_candles_in_trade:
        return "TIME_EXIT"

    return None
