"""
Performance metrics for backtesting.
Per the backtesting-framework skill.
"""
from __future__ import annotations

from collections import Counter
from math import sqrt
from typing import Optional


MINIMUM_THRESHOLDS = {
    "total_trades": 200,
    "win_rate": 0.52,
    "sharpe_ratio": 1.0,
    "max_drawdown": 0.20,
    "profit_factor": 1.2,
}


def compute_metrics(trades: list[dict], equity_curve: list[float],
                    initial_bankroll: float) -> dict:
    if not trades:
        return {"total_trades": 0, "valid": False}

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

    metrics = {
        "total_trades": len(trades),
        "trades_per_day": round(len(trades) / total_days, 2),
        "win_rate": round(len(winners) / len(trades), 4) if trades else 0,
        "avg_win_pct": round(sum(winners) / len(winners), 4) if winners else 0,
        "avg_loss_pct": round(sum(losers) / len(losers), 4) if losers else 0,
        "profit_factor": round(
            sum(winners) / abs(sum(losers)), 4
        )
        if losers and sum(losers) != 0
        else float("inf"),
        "total_return_pct": round(
            (equity_curve[-1] - initial_bankroll) / initial_bankroll * 100, 2
        )
        if equity_curve
        else 0,
        "sharpe_ratio": round(mean_ret / std_ret * sqrt(252), 4) if std_ret > 1e-9 else 0,
        "max_drawdown_pct": round(_max_drawdown(equity_curve) * 100, 2),
        "exit_reasons": dict(Counter(t.get("exit_reason", "unknown") for t in trades)),
        "avg_candles_held": round(
            sum(t.get("candles_held", 0) for t in trades) / len(trades), 2
        ),
    }

    metrics["passes_minimum"] = all(
        metrics.get(k, 0) >= v if k != "max_drawdown"
        else metrics.get("max_drawdown_pct", 100) / 100 <= v
        for k, v in MINIMUM_THRESHOLDS.items()
    )

    return metrics


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
