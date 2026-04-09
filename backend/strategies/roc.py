"""
ROC (Rate of Change) momentum strategy — confirmation signal.
Per the roc-trading skill.
"""
from __future__ import annotations

from typing import Optional

from config import settings
from strategies.obi import Direction


def calculate_roc(closes: list[float], lookback: int) -> Optional[float]:
    if len(closes) < lookback + 1:
        return None
    old = closes[-(lookback + 1)]
    if old == 0:
        return None
    return ((closes[-1] - old) / old) * 100


def candle_direction_count(candles: list[dict], direction: str) -> int:
    """Count how many of last 3 candles closed in given direction."""
    recent = candles[-3:]
    if direction == "up":
        return sum(1 for c in recent if c["close"] > c["open"])
    return sum(1 for c in recent if c["close"] < c["open"])


def evaluate_roc(
    closes: list[float],
    candles: list[dict],
    atr_regime: str,
    obi_direction: Direction,
    has_position: bool,
) -> Direction:
    """Evaluate ROC signal direction."""
    cfg = settings.roc

    if has_position:
        return Direction.NEUTRAL

    if atr_regime == "LOW":
        return Direction.NEUTRAL

    roc = calculate_roc(closes, cfg.lookback)
    if roc is None:
        return Direction.NEUTRAL

    if roc >= cfg.long_threshold and roc <= cfg.max_cap:
        if candle_direction_count(candles, "up") >= cfg.candle_confirm_min:
            if obi_direction != Direction.SHORT:
                return Direction.LONG

    if roc <= cfg.short_threshold and roc >= cfg.min_cap:
        if candle_direction_count(candles, "down") >= cfg.candle_confirm_min:
            if obi_direction != Direction.LONG:
                return Direction.SHORT

    return Direction.NEUTRAL


def check_roc_exit(
    direction: str,
    pnl_pct: float,
    entry_roc: float,
    current_roc: Optional[float],
    latest_candle: Optional[dict],
    candles_held: int,
) -> Optional[str]:
    """Check ROC-specific exit conditions."""
    cfg = settings.roc
    risk = settings.risk

    if pnl_pct <= -risk.stop_loss_pct:
        return "STOP_LOSS"

    if pnl_pct >= risk.stop_loss_pct * risk.profit_target_mult:
        return "TAKE_PROFIT"

    if latest_candle and pnl_pct > 0:
        candle_move = (
            abs(latest_candle["close"] - latest_candle["open"])
            / latest_candle["open"]
            * 100
        )
        if candle_move >= cfg.blowoff_single_candle:
            return "BLOWOFF_TAKE_PROFIT"

    if current_roc is not None and entry_roc != 0:
        if abs(current_roc) < abs(entry_roc) * cfg.momentum_stall_ratio:
            return "MOMENTUM_STALL"

    if latest_candle:
        if direction == "long" and latest_candle["close"] < latest_candle["open"]:
            return "CANDLE_REVERSAL"
        if direction == "short" and latest_candle["close"] > latest_candle["open"]:
            return "CANDLE_REVERSAL"

    if candles_held >= cfg.max_candles_in_trade:
        return "TIME_EXIT"

    return None
