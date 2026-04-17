"""Unit tests for trend-aware short suppression."""

from filters.trend_guard import TrendGuard
from strategies.obi import Direction
from strategies.resolver import Conviction, TradeDecision


def _short_decision(conviction: Conviction) -> TradeDecision:
    return TradeDecision(
        direction=Direction.SHORT,
        conviction=conviction,
        obi_dir=Direction.SHORT,
        roc_dir=Direction.NEUTRAL,
        skip_reason=None,
    )


def test_short_blocked_in_strong_uptrend():
    guard = TrendGuard(lookback_candles=4, soften_rise_pct=0.2, block_rise_pct=0.35)
    decision = _short_decision(Conviction.NORMAL)
    closes = [100.0, 100.1, 100.2, 100.5]  # +0.5%
    reason = guard.apply_short_trend_filter(decision, closes, mode="paper")
    assert reason is not None
    assert decision.direction is None
    assert decision.conviction == Conviction.NONE
    assert decision.skip_reason is not None
    assert decision.skip_reason.startswith("SHORT_BLOCKED_UPTREND")


def test_short_high_conviction_downgraded_on_mild_uptrend():
    guard = TrendGuard(lookback_candles=4, soften_rise_pct=0.2, block_rise_pct=0.5)
    decision = _short_decision(Conviction.HIGH)
    closes = [100.0, 100.05, 100.15, 100.25]  # +0.25%
    reason = guard.apply_short_trend_filter(decision, closes, mode="live")
    assert reason is None
    assert decision.direction == Direction.SHORT
    assert decision.conviction == Conviction.NORMAL
    assert decision.skip_reason is None


def test_short_normal_conviction_downgraded_to_low_on_mild_uptrend():
    guard = TrendGuard(lookback_candles=4, soften_rise_pct=0.2, block_rise_pct=0.5)
    decision = _short_decision(Conviction.NORMAL)
    closes = [100.0, 100.05, 100.12, 100.21]  # +0.21%
    guard.apply_short_trend_filter(decision, closes, mode="live")
    assert decision.direction == Direction.SHORT
    assert decision.conviction == Conviction.LOW


def test_short_low_conviction_skipped_by_trend_guard():
    """LOW conviction trades have should_trade=False, so trend guard is a no-op."""
    guard = TrendGuard(lookback_candles=4, soften_rise_pct=0.2, block_rise_pct=0.5)
    decision = _short_decision(Conviction.LOW)
    closes = [100.0, 100.06, 100.12, 100.22]  # +0.22%
    reason = guard.apply_short_trend_filter(decision, closes, mode="paper")
    assert reason is None
    assert decision.conviction == Conviction.LOW
    assert decision.should_trade is False


def test_noop_for_long_decision():
    guard = TrendGuard(lookback_candles=4, soften_rise_pct=0.2, block_rise_pct=0.35)
    decision = TradeDecision(
        direction=Direction.LONG,
        conviction=Conviction.NORMAL,
        obi_dir=Direction.LONG,
        roc_dir=Direction.NEUTRAL,
        skip_reason=None,
    )
    closes = [100.0, 100.1, 100.2, 100.5]
    reason = guard.apply_short_trend_filter(decision, closes, mode="live")
    assert reason is None
    assert decision.direction == Direction.LONG
    assert decision.conviction == Conviction.NORMAL


def test_noop_when_not_enough_candles():
    guard = TrendGuard(lookback_candles=5, soften_rise_pct=0.2, block_rise_pct=0.35)
    decision = _short_decision(Conviction.NORMAL)
    closes = [100.0, 100.1, 100.2]
    reason = guard.apply_short_trend_filter(decision, closes, mode="live")
    assert reason is None
    assert decision.direction == Direction.SHORT
    assert decision.conviction == Conviction.NORMAL
