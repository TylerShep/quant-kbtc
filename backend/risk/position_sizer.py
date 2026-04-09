"""
Position sizing — fixed fractional with conviction multipliers.
Per the risk-position-sizing skill.
"""
from __future__ import annotations

from config import settings


class PositionSizer:
    def __init__(self, initial_bankroll: float):
        self.bankroll = initial_bankroll
        self.peak_bankroll = initial_bankroll
        self.daily_start_bankroll = initial_bankroll
        self.weekly_start_bankroll = initial_bankroll
        self.trades_today: list[float] = []
        self.trades_this_week: list[float] = []

    def calculate_size(self, conviction: str) -> float:
        """Returns dollar amount to risk on this trade."""
        cfg = settings.risk
        base_risk = self.bankroll * cfg.risk_per_trade_pct

        multipliers = {
            "HIGH": cfg.high_conviction_mult,
            "NORMAL": cfg.normal_conviction_mult,
            "LOW": cfg.low_conviction_mult,
        }
        adjusted = base_risk * multipliers.get(conviction, 1.0)

        if self.current_drawdown > 0.10:
            adjusted *= cfg.drawdown_reduction_mult

        max_risk = self.bankroll * cfg.max_risk_per_trade_pct
        min_risk = self.bankroll * cfg.min_risk_per_trade_pct
        return round(max(min_risk, min(max_risk, adjusted)), 2)

    def record_trade(self, pnl: float):
        self.bankroll += pnl
        self.trades_today.append(pnl)
        self.trades_this_week.append(pnl)

    def reset_daily(self):
        self.daily_start_bankroll = self.bankroll
        self.trades_today = []

    def reset_weekly(self):
        self.weekly_start_bankroll = self.bankroll
        self.trades_this_week = []

    @property
    def current_drawdown(self) -> float:
        self.peak_bankroll = max(self.peak_bankroll, self.bankroll)
        if self.peak_bankroll == 0:
            return 0.0
        return (self.peak_bankroll - self.bankroll) / self.peak_bankroll

    @property
    def daily_loss(self) -> float:
        if self.daily_start_bankroll == 0:
            return 0.0
        return (self.daily_start_bankroll - self.bankroll) / self.daily_start_bankroll

    @property
    def weekly_loss(self) -> float:
        if self.weekly_start_bankroll == 0:
            return 0.0
        return (self.weekly_start_bankroll - self.bankroll) / self.weekly_start_bankroll

    def get_state(self) -> dict:
        return {
            "bankroll": round(self.bankroll, 2),
            "peak_bankroll": round(self.peak_bankroll, 2),
            "drawdown_pct": round(self.current_drawdown * 100, 2),
            "daily_loss_pct": round(self.daily_loss * 100, 2),
            "weekly_loss_pct": round(self.weekly_loss * 100, 2),
            "trades_today": len(self.trades_today),
        }
