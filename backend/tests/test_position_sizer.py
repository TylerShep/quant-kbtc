"""Unit tests for PositionSizer."""
from config import settings
from risk.position_sizer import PositionSizer


def test_calculate_size_scales_with_conviction():
    bankroll = 10_000.0
    sizer = PositionSizer(bankroll)
    cfg = settings.risk
    base = bankroll * cfg.risk_per_trade_pct
    high = round(max(cfg.min_risk_per_trade_pct * bankroll, min(cfg.max_risk_per_trade_pct * bankroll, base * cfg.high_conviction_mult)), 2)
    normal = round(max(cfg.min_risk_per_trade_pct * bankroll, min(cfg.max_risk_per_trade_pct * bankroll, base * cfg.normal_conviction_mult)), 2)
    low = round(max(cfg.min_risk_per_trade_pct * bankroll, min(cfg.max_risk_per_trade_pct * bankroll, base * cfg.low_conviction_mult)), 2)
    assert sizer.calculate_size("HIGH") == high
    assert sizer.calculate_size("NORMAL") == normal
    assert sizer.calculate_size("LOW") == low
    assert high >= normal >= low


def test_record_trade_updates_bankroll():
    sizer = PositionSizer(1000.0)
    sizer.record_trade(50.0)
    assert sizer.bankroll == 1050.0
    sizer.record_trade(-30.0)
    assert sizer.bankroll == 1020.0


def test_current_drawdown_after_peak_and_loss():
    sizer = PositionSizer(1000.0)
    sizer.record_trade(500.0)
    _ = sizer.current_drawdown
    assert sizer.peak_bankroll >= 1500.0
    sizer.record_trade(-300.0)
    assert sizer.bankroll == 1200.0
    assert abs(sizer.current_drawdown - (1500.0 - 1200.0) / 1500.0) < 1e-9


def test_drawdown_reduction_multiplier_applied():
    sizer = PositionSizer(10_000.0)
    sizer.bankroll = 10_000.0
    sizer.peak_bankroll = 12_000.0
    assert sizer.current_drawdown > 0.10
    cfg = settings.risk
    base_size = sizer.calculate_size("NORMAL")
    sizer.peak_bankroll = 10_000.0
    sizer.bankroll = 10_000.0
    no_dd_size = sizer.calculate_size("NORMAL")
    assert base_size == round(no_dd_size * cfg.drawdown_reduction_mult, 2)
