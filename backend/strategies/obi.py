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
    overrides: Optional[dict] = None,
) -> Direction:
    """
    Evaluate OBI signal direction.
    Returns LONG, SHORT, or NEUTRAL.
    overrides: optional dict to override settings.obi fields for backtesting.
    """
    cfg = settings.obi
    ov = overrides or {}

    if has_position:
        return Direction.NEUTRAL

    if atr_regime == "HIGH":
        return Direction.NEUTRAL

    min_vol = ov.get("min_book_volume", cfg.min_book_volume)
    if total_book_volume < min_vol:
        return Direction.NEUTRAL

    n = ov.get("consecutive_readings", cfg.consecutive_readings)
    if len(obi_history) < n:
        return Direction.NEUTRAL

    recent = obi_history[-n:]

    long_thresh = ov.get("long_threshold", cfg.long_threshold)
    short_thresh = ov.get("short_threshold", cfg.short_threshold)

    if all(r >= long_thresh for r in recent):
        return Direction.LONG

    if all(r <= short_thresh for r in recent):
        return Direction.SHORT

    return Direction.NEUTRAL


def check_obi_exit(
    direction: str,
    current_obi: float,
    pnl_pct: float,
    candles_held: int,
    atr_regime: str,
    overrides: Optional[dict] = None,
) -> Optional[str]:
    """Check OBI-specific exit conditions. Returns exit reason or None."""
    cfg = settings.obi
    risk = settings.risk
    ov = overrides or {}

    stop_loss = ov.get("stop_loss_pct", risk.stop_loss_pct)
    profit_mult = ov.get("profit_target_mult", risk.profit_target_mult)

    if direction == "short":
        short_mult = ov.get("short_stop_loss_mult", risk.short_stop_loss_mult)
        stop_loss *= short_mult

    if pnl_pct <= -stop_loss:
        return "STOP_LOSS"

    if pnl_pct >= stop_loss * profit_mult:
        return "TAKE_PROFIT"

    min_hold = ov.get("min_candles_before_early_exit", risk.min_candles_before_early_exit)
    early_exit_ok = candles_held >= min_hold or pnl_pct < 0

    neutral_long = ov.get("neutral_exit_long", cfg.neutral_exit_long)
    neutral_short = ov.get("neutral_exit_short", cfg.neutral_exit_short)

    if early_exit_ok:
        if direction == "long" and current_obi < neutral_long:
            return "SIGNAL_DECAY"

        if direction == "short" and current_obi > neutral_short:
            return "SIGNAL_DECAY"

    if atr_regime == "HIGH":
        return "VOLATILITY_SPIKE"

    max_candles = ov.get("max_candles_in_trade", cfg.max_candles_in_trade)
    if candles_held >= max_candles:
        return "TIME_EXIT"

    return None
