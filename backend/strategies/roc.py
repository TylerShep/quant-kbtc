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
    overrides: Optional[dict] = None,
) -> Direction:
    """Evaluate ROC signal direction. overrides: optional dict for backtesting."""
    cfg = settings.roc
    ov = overrides or {}

    if has_position:
        return Direction.NEUTRAL

    if atr_regime == "LOW":
        return Direction.NEUTRAL

    lookback = ov.get("roc_lookback", cfg.lookback)
    roc = calculate_roc(closes, lookback)
    if roc is None:
        return Direction.NEUTRAL

    long_thresh = ov.get("roc_long_threshold", cfg.long_threshold)
    short_thresh = ov.get("roc_short_threshold", cfg.short_threshold)
    max_cap = ov.get("roc_max_cap", cfg.max_cap)
    min_cap = ov.get("roc_min_cap", cfg.min_cap)
    confirm_min = ov.get("roc_candle_confirm_min", cfg.candle_confirm_min)

    if roc >= long_thresh and roc <= max_cap:
        if candle_direction_count(candles, "up") >= confirm_min:
            if obi_direction != Direction.SHORT:
                return Direction.LONG

    if roc <= short_thresh and roc >= min_cap:
        if candle_direction_count(candles, "down") >= confirm_min:
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
    overrides: Optional[dict] = None,
) -> Optional[str]:
    """Check ROC-specific exit conditions. overrides: optional dict for backtesting."""
    cfg = settings.roc
    risk = settings.risk
    ov = overrides or {}

    stop_loss = ov.get("stop_loss_pct", risk.stop_loss_pct)
    profit_mult = ov.get("profit_target_mult", risk.profit_target_mult)

    if pnl_pct <= -stop_loss:
        return "STOP_LOSS"

    if pnl_pct >= stop_loss * profit_mult:
        return "TAKE_PROFIT"

    blowoff = ov.get("roc_blowoff_single_candle", cfg.blowoff_single_candle)
    if latest_candle and pnl_pct > 0:
        candle_move = (
            abs(latest_candle["close"] - latest_candle["open"])
            / latest_candle["open"]
            * 100
        )
        if candle_move >= blowoff:
            return "BLOWOFF_TAKE_PROFIT"

    stall_ratio = ov.get("roc_momentum_stall_ratio", cfg.momentum_stall_ratio)
    if current_roc is not None and entry_roc != 0:
        if abs(current_roc) < abs(entry_roc) * stall_ratio:
            return "MOMENTUM_STALL"

    if latest_candle:
        if direction == "long" and latest_candle["close"] < latest_candle["open"]:
            return "CANDLE_REVERSAL"
        if direction == "short" and latest_candle["close"] > latest_candle["open"]:
            return "CANDLE_REVERSAL"

    max_candles = ov.get("roc_max_candles_in_trade", cfg.max_candles_in_trade)
    if candles_held >= max_candles:
        return "TIME_EXIT"

    return None
