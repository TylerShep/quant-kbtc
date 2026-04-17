"""
Autonomous parameter tuner — runs walk-forward optimization and recommends
or auto-applies parameter changes with safety rails.

Can be run as a cron job or triggered manually via CLI.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from config import settings

logger = structlog.get_logger(__name__)

MAX_SHIFT = {
    "long_threshold": 0.05,
    "short_threshold": 0.05,
    "roc_lookback": 1,
    "roc_long_threshold": 0.1,
    "roc_short_threshold": 0.1,
    "risk_per_trade_pct": 0.005,
    "stop_loss_pct": 0.005,
    "profit_target_mult": 0.25,
}

MIN_EDGE_CONSISTENCY = 0.5
MIN_OOS_SHARPE = 0.8


@dataclass
class TuningResult:
    timestamp: float
    current_params: dict
    recommended_params: dict
    edge_consistency: float
    avg_oos_sharpe: float
    should_apply: bool
    reason: str
    changes: dict = field(default_factory=dict)


def get_current_params() -> dict:
    """Extract current live parameters from settings."""
    return {
        "long_threshold": settings.obi.long_threshold,
        "short_threshold": settings.obi.short_threshold,
        "consecutive_readings": settings.obi.consecutive_readings,
        "min_book_volume": settings.obi.min_book_volume,
        "roc_lookback": settings.roc.lookback,
        "roc_long_threshold": settings.roc.long_threshold,
        "roc_short_threshold": settings.roc.short_threshold,
        "risk_per_trade_pct": settings.risk.risk_per_trade_pct,
        "stop_loss_pct": settings.risk.stop_loss_pct,
        "profit_target_mult": settings.risk.profit_target_mult,
    }


def build_param_space(current: dict) -> dict:
    """Build a search grid centered around current params."""
    def _range(val, step, n=2):
        return sorted(set(round(val + step * i, 6) for i in range(-n, n + 1)))

    return {
        "risk_per_trade_pct": _range(current["risk_per_trade_pct"], 0.005),
        "stop_loss_pct": _range(current["stop_loss_pct"], 0.005),
        "long_threshold": _range(current["long_threshold"], 0.025),
        "short_threshold": _range(current["short_threshold"], 0.025),
        "roc_lookback": list(range(max(1, current["roc_lookback"] - 2),
                                   current["roc_lookback"] + 3)),
    }


def clamp_params(current: dict, recommended: dict) -> dict:
    """Apply maximum shift limits per parameter."""
    clamped = {}
    for key, new_val in recommended.items():
        if key in current and key in MAX_SHIFT:
            old_val = current[key]
            max_delta = MAX_SHIFT[key]
            if isinstance(new_val, (int, float)):
                delta = new_val - old_val
                if abs(delta) > max_delta:
                    clamped_val = old_val + max_delta * (1 if delta > 0 else -1)
                    clamped[key] = type(old_val)(round(clamped_val, 6))
                else:
                    clamped[key] = new_val
            else:
                clamped[key] = new_val
        else:
            clamped[key] = new_val
    return clamped


def evaluate_recommendation(
    current: dict,
    recommended: dict,
    edge_consistency: float,
    avg_oos_sharpe: float,
) -> TuningResult:
    """Decide whether to apply recommended params."""
    changes = {}
    for key in recommended:
        if key in current and recommended[key] != current[key]:
            changes[key] = {"from": current[key], "to": recommended[key]}

    if not changes:
        return TuningResult(
            timestamp=time.time(),
            current_params=current,
            recommended_params=recommended,
            edge_consistency=edge_consistency,
            avg_oos_sharpe=avg_oos_sharpe,
            should_apply=False,
            reason="No parameter changes needed",
            changes=changes,
        )

    if edge_consistency < MIN_EDGE_CONSISTENCY:
        return TuningResult(
            timestamp=time.time(),
            current_params=current,
            recommended_params=recommended,
            edge_consistency=edge_consistency,
            avg_oos_sharpe=avg_oos_sharpe,
            should_apply=False,
            reason=f"Edge consistency {edge_consistency:.1%} below threshold {MIN_EDGE_CONSISTENCY:.1%}",
            changes=changes,
        )

    if avg_oos_sharpe < MIN_OOS_SHARPE:
        return TuningResult(
            timestamp=time.time(),
            current_params=current,
            recommended_params=recommended,
            edge_consistency=edge_consistency,
            avg_oos_sharpe=avg_oos_sharpe,
            should_apply=False,
            reason=f"OOS Sharpe {avg_oos_sharpe:.2f} below threshold {MIN_OOS_SHARPE:.2f}",
            changes=changes,
        )

    clamped = clamp_params(current, recommended)
    return TuningResult(
        timestamp=time.time(),
        current_params=current,
        recommended_params=clamped,
        edge_consistency=edge_consistency,
        avg_oos_sharpe=avg_oos_sharpe,
        should_apply=True,
        reason=f"Recommended: consistency={edge_consistency:.1%}, OOS Sharpe={avg_oos_sharpe:.2f}",
        changes={k: {"from": current.get(k), "to": clamped[k]}
                 for k in clamped if k in current and clamped[k] != current[k]},
    )


async def persist_recommendation(pool, result: TuningResult) -> None:
    """Save tuning recommendation to the database."""
    try:
        async with pool.connection() as conn:
            await conn.execute(
                """INSERT INTO param_recommendations
                   (timestamp, current_params, recommended_params, edge_consistency,
                    avg_oos_sharpe, should_apply, reason, changes)
                   VALUES (NOW(), %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s::jsonb)""",
                (
                    json.dumps(result.current_params),
                    json.dumps(result.recommended_params),
                    result.edge_consistency,
                    result.avg_oos_sharpe,
                    result.should_apply,
                    result.reason,
                    json.dumps(result.changes),
                ),
            )
    except Exception as e:
        logger.error("auto_tuner.persist_failed", error=str(e))


async def run_tuning_cycle(
    candles: list[dict],
    ob_history: dict,
    pool=None,
    auto_apply: bool = False,
) -> TuningResult:
    """Run a full tuning cycle: walk-forward -> evaluate -> recommend/apply."""
    from backtesting.walk_forward import WalkForwardOptimizer
    from backtesting.data_loader import load_kalshi_markets_db, load_tfi_history_db

    current = get_current_params()
    param_space = build_param_space(current)

    logger.info("auto_tuner.starting", candle_count=len(candles))

    settlement_data: dict = {}
    tfi_history: dict = {}
    if pool:
        try:
            settlement_data = await load_kalshi_markets_db(pool)
            tfi_history = await load_tfi_history_db(pool)
            logger.info("auto_tuner.data_loaded",
                        settlements=len(settlement_data),
                        tfi_points=len(tfi_history))
        except Exception as e:
            logger.warning("auto_tuner.data_load_failed", error=str(e))

    optimizer = WalkForwardOptimizer(
        candles, ob_history,
        settlement_data=settlement_data,
        tfi_history=tfi_history,
    )
    results = optimizer.run(param_space, objective="sharpe_ratio")

    if not results:
        return TuningResult(
            timestamp=time.time(),
            current_params=current,
            recommended_params=current,
            edge_consistency=0,
            avg_oos_sharpe=0,
            should_apply=False,
            reason="Walk-forward produced no valid windows",
        )

    consistency = optimizer.edge_consistency(results)
    avg_sharpe = sum(r.test_sharpe for r in results) / len(results)
    recommended = optimizer.select_final_params(results) or current

    result = evaluate_recommendation(current, recommended, consistency, avg_sharpe)

    if pool:
        await persist_recommendation(pool, result)

    if result.should_apply and auto_apply:
        logger.info("auto_tuner.applying", changes=result.changes)
        if pool:
            await apply_params(pool, result.recommended_params)
    else:
        logger.info("auto_tuner.recommendation",
                     should_apply=result.should_apply,
                     reason=result.reason,
                     changes=result.changes)

    return result


async def apply_params(pool, recommended: dict) -> None:
    """Write recommended params to bot_state as param_overrides.

    The coordinator reads this key on startup and on every _save_state cycle,
    passing the values as ``overrides`` to strategy evaluation functions.
    """
    try:
        async with pool.connection() as conn:
            await conn.execute(
                """INSERT INTO bot_state (key, value, updated_at)
                   VALUES ('param_overrides', %s::jsonb, NOW())
                   ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value, updated_at = NOW()""",
                (json.dumps(recommended),),
            )
        logger.info("auto_tuner.params_applied", params=recommended)
    except Exception as e:
        logger.error("auto_tuner.apply_failed", error=str(e))


async def get_applied_params(pool) -> dict | None:
    """Read the currently active param_overrides from bot_state."""
    try:
        async with pool.connection() as conn:
            row = await conn.execute(
                "SELECT value FROM bot_state WHERE key = 'param_overrides'"
            )
            result = await row.fetchone()
            if result:
                val = result[0]
                return val if isinstance(val, dict) else json.loads(val)
    except Exception as e:
        logger.error("auto_tuner.get_params_failed", error=str(e))
    return None


async def clear_applied_params(pool) -> None:
    """Delete param_overrides from bot_state, reverting to defaults."""
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM bot_state WHERE key = 'param_overrides'"
            )
        logger.info("auto_tuner.params_cleared")
    except Exception as e:
        logger.error("auto_tuner.clear_failed", error=str(e))


def run_tuning_sync(csv_path: str, output_dir: str = "backtest_reports") -> TuningResult:
    """Synchronous wrapper for CLI usage."""
    from backtesting.data_loader import load_candles_csv

    candles = load_candles_csv(csv_path)
    result = asyncio.run(run_tuning_cycle(candles, {}))

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "tuning_latest.json", "w") as f:
        json.dump({
            "timestamp": result.timestamp,
            "current_params": result.current_params,
            "recommended_params": result.recommended_params,
            "edge_consistency": result.edge_consistency,
            "avg_oos_sharpe": result.avg_oos_sharpe,
            "should_apply": result.should_apply,
            "reason": result.reason,
            "changes": result.changes,
        }, f, indent=2)

    return result
