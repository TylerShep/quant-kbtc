"""
Performance metrics for backtesting.
Per the backtesting-framework and cost-fee-optimizer skills.
"""
from __future__ import annotations

from collections import Counter
from math import sqrt


MINIMUM_THRESHOLDS = {
    "total_trades": 200,
    "win_rate": 0.52,
    "sharpe_ratio": 1.0,
    "max_drawdown": 0.20,
    "profit_factor": 1.2,
}

OVERFITTING_RED_FLAGS = {
    "sharpe_too_high": 3.0,
    "win_rate_too_high": 0.70,
    "too_few_trades": 50,
}

FEE_RATE = 0.007


def compute_metrics(trades: list[dict], equity_curve: list[float],
                    initial_bankroll: float) -> dict:
    if not trades:
        return {
            "total_trades": 0,
            "valid": False,
            "win_rate": 0,
            "sharpe_ratio": 0,
            "sortino_ratio": 0,
            "max_drawdown_pct": 0,
            "profit_factor": 0,
            "total_return_pct": 0,
            "recovery_factor": 0,
            "breakeven_win_rate": 0,
            "avg_win_pct": 0,
            "avg_loss_pct": 0,
            "best_trade_pct": 0,
            "worst_trade_pct": 0,
            "total_pnl": 0,
            "total_fees": 0,
            "total_days": 0,
            "trades_per_day": 0,
            "avg_candles_held": 0,
            "exit_reasons": {},
            "regime_breakdown": {},
            "passes_minimum": False,
            "overfitting_red_flags": {},
        }

    returns = [t["pnl_pct"] for t in trades]
    winners = [r for r in returns if r > 0]
    losers = [r for r in returns if r <= 0]

    total_days = 1
    if len(trades) > 1:
        total_days = max(
            1,
            (trades[-1].get("exit_timestamp", 0) - trades[0].get("timestamp", 0)) / 86400,
        )

    mean_ret = sum(returns) / len(returns) if returns else 0
    std_ret = _std(returns) if len(returns) > 1 else 1e-9

    trades_per_day = len(trades) / total_days if total_days > 0 else 0
    trades_per_year = trades_per_day * 365
    annualization_factor = sqrt(max(trades_per_year, 1))

    # Sortino: uses downside deviation only
    downside_returns = [r for r in returns if r < 0]
    downside_std = _std(downside_returns) if len(downside_returns) > 1 else 1e-9
    sortino = round(mean_ret / downside_std * annualization_factor, 4) if downside_std > 1e-9 else 0

    sharpe = round(mean_ret / std_ret * annualization_factor, 4) if std_ret > 1e-9 else 0

    max_dd = _max_drawdown(equity_curve)
    total_return_pct = round(
        (equity_curve[-1] - initial_bankroll) / initial_bankroll * 100, 2
    ) if equity_curve else 0

    # Recovery factor: total return / max drawdown
    recovery_factor = round(
        abs(total_return_pct / 100) / max_dd, 2
    ) if max_dd > 0 else float("inf")

    win_rate = round(len(winners) / len(trades), 4) if trades else 0
    avg_win = round(sum(winners) / len(winners), 4) if winners else 0
    avg_loss = round(sum(losers) / len(losers), 4) if losers else 0

    # Break-even win rate after fees (from cost-fee-optimizer skill)
    net_win = abs(avg_win) - FEE_RATE if avg_win else 0
    net_loss = abs(avg_loss) + FEE_RATE if avg_loss else 0
    breakeven_wr = round(net_loss / (net_win + net_loss), 4) if (net_win + net_loss) > 0 else 0.5

    regime_stats = _regime_breakdown(trades)

    # Overfitting red flags
    red_flags = {
        "sharpe_too_high": sharpe > OVERFITTING_RED_FLAGS["sharpe_too_high"],
        "win_rate_too_high": win_rate > OVERFITTING_RED_FLAGS["win_rate_too_high"],
        "too_few_trades": len(trades) < OVERFITTING_RED_FLAGS["too_few_trades"],
    }

    metrics = {
        "total_trades": len(trades),
        "trades_per_day": round(trades_per_day, 2),
        "win_rate": win_rate,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "best_trade_pct": round(max(returns), 4) if returns else 0,
        "worst_trade_pct": round(min(returns), 4) if returns else 0,
        "profit_factor": round(
            sum(winners) / abs(sum(losers)), 4
        ) if losers and sum(losers) != 0 else float("inf"),
        "total_return_pct": total_return_pct,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "recovery_factor": recovery_factor,
        "breakeven_win_rate": breakeven_wr,
        "exit_reasons": dict(Counter(t.get("exit_reason", "unknown") for t in trades)),
        "avg_candles_held": round(
            sum(t.get("candles_held", 0) for t in trades) / len(trades), 2
        ),
        "regime_breakdown": regime_stats,
        "total_pnl": round(sum(t["pnl"] for t in trades), 4),
        "total_fees": round(sum(t["fees"] for t in trades), 4),
        "total_days": round(total_days, 1),
        "overfitting_red_flags": red_flags,
    }

    metrics["passes_minimum"] = all(
        metrics.get(k, 0) >= v if k != "max_drawdown"
        else metrics.get("max_drawdown_pct", 100) / 100 <= v
        for k, v in MINIMUM_THRESHOLDS.items()
    )

    return metrics


def _regime_breakdown(trades: list[dict]) -> dict:
    """Win rate and trade count by ATR regime."""
    by_regime: dict[str, dict] = {}
    for t in trades:
        regime = t.get("regime_at_entry", "UNKNOWN")
        if regime not in by_regime:
            by_regime[regime] = {"total": 0, "wins": 0}
        by_regime[regime]["total"] += 1
        if t["pnl"] > 0:
            by_regime[regime]["wins"] += 1

    return {
        regime: {
            "total": stats["total"],
            "wins": stats["wins"],
            "win_rate": round(stats["wins"] / stats["total"], 4) if stats["total"] > 0 else 0,
        }
        for regime, stats in by_regime.items()
    }


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
    return sqrt(variance)


def _max_drawdown(equity: list[float]) -> float:
    if not equity:
        return 0
    peak = equity[0]
    max_dd = 0.0
    for val in equity:
        peak = max(peak, val)
        dd = (peak - val) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    return max_dd
