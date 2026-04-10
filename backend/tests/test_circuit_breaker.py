"""Unit tests for CircuitBreaker."""
from config import settings
from risk.circuit_breaker import CircuitBreaker
from risk.position_sizer import PositionSizer


def test_can_trade_true_when_within_limits():
    sizer = PositionSizer(10_000.0)
    sizer.reset_daily()
    sizer.reset_weekly()
    breaker = CircuitBreaker(sizer)
    ok, reason = breaker.can_trade()
    assert ok is True
    assert reason is None


def test_daily_loss_limit_trips():
    sizer = PositionSizer(10_000.0)
    sizer.reset_daily()
    limit = settings.risk.daily_loss_limit_pct
    loss = sizer.daily_start_bankroll * (limit + 0.01)
    sizer.record_trade(-loss)
    breaker = CircuitBreaker(sizer)
    ok, reason = breaker.can_trade()
    assert ok is False
    assert reason == "DAILY_LOSS_LIMIT"


def test_weekly_loss_limit_trips():
    sizer = PositionSizer(10_000.0)
    sizer.bankroll = 8400.0
    sizer.weekly_start_bankroll = 10_000.0
    sizer.daily_start_bankroll = 8700.0
    breaker = CircuitBreaker(sizer)
    ok, reason = breaker.can_trade()
    assert ok is False
    assert reason == "WEEKLY_LOSS_LIMIT"


def test_max_drawdown_trips():
    sizer = PositionSizer(10_000.0)
    sizer.peak_bankroll = 10_000.0
    sizer.bankroll = 7900.0
    sizer.daily_start_bankroll = 8000.0
    sizer.weekly_start_bankroll = 8000.0
    breaker = CircuitBreaker(sizer)
    ok, reason = breaker.can_trade()
    assert ok is False
    assert reason == "MAX_DRAWDOWN_HALT"
