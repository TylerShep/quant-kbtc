"""
Signal decay monitoring — IC calculation, win rate/Sharpe drift detection.
Per the quantitative-researcher skill's signal decay monitoring spec.
"""
from __future__ import annotations

import json
import time

import structlog

logger = structlog.get_logger(__name__)

DECAY_THRESHOLDS = {
    "ic_floor": 0.03,
    "win_rate_drop": 0.05,
    "sharpe_drop": 0.30,
}

BASELINE_KEY = "signal_baseline"


def compute_signal_ic(signal_values: list[float], forward_returns: list[float]) -> float:
    """Spearman rank correlation between signal values and forward returns.

    IC > 0.05 = tradeable signal. IC > 0.10 = strong signal.
    Uses a simple rank-correlation implementation to avoid scipy dependency.
    """
    n = len(signal_values)
    if n < 10 or n != len(forward_returns):
        return 0.0

    def _ranks(vals: list[float]) -> list[float]:
        indexed = sorted(enumerate(vals), key=lambda x: x[1])
        ranks = [0.0] * n
        for rank_pos, (orig_idx, _) in enumerate(indexed):
            ranks[orig_idx] = float(rank_pos + 1)
        return ranks

    sig_ranks = _ranks(signal_values)
    ret_ranks = _ranks(forward_returns)

    d_sq_sum = sum((s - r) ** 2 for s, r in zip(sig_ranks, ret_ranks))
    rho = 1.0 - (6.0 * d_sq_sum) / (n * (n * n - 1))
    return round(rho, 4)


def check_signal_health(live_stats: dict, baseline: dict) -> list[str]:
    """Compare live stats against baseline and return alert messages.

    ``live_stats`` and ``baseline`` should each contain:
        ic, win_rate, sharpe
    """
    alerts: list[str] = []

    live_ic = live_stats.get("ic", 0)
    if live_ic < DECAY_THRESHOLDS["ic_floor"]:
        alerts.append(f"IC below floor ({live_ic:.3f} < {DECAY_THRESHOLDS['ic_floor']})")

    baseline_wr = baseline.get("win_rate", 0)
    live_wr = live_stats.get("win_rate", 0)
    if baseline_wr - live_wr > DECAY_THRESHOLDS["win_rate_drop"]:
        alerts.append(
            f"Win rate decay ({live_wr:.1%} vs baseline {baseline_wr:.1%}, "
            f"drop {baseline_wr - live_wr:.1%})"
        )

    baseline_sharpe = baseline.get("sharpe", 0)
    live_sharpe = live_stats.get("sharpe", 0)
    if baseline_sharpe > 0:
        sharpe_decline = (baseline_sharpe - live_sharpe) / baseline_sharpe
        if sharpe_decline > DECAY_THRESHOLDS["sharpe_drop"]:
            alerts.append(
                f"Sharpe decay ({live_sharpe:.2f} vs baseline {baseline_sharpe:.2f}, "
                f"decline {sharpe_decline:.0%})"
            )

    return alerts


async def _get_baseline(pool) -> dict | None:
    """Load stored baseline from bot_state."""
    try:
        async with pool.connection() as conn:
            row = await conn.execute(
                "SELECT value FROM bot_state WHERE key = %s", (BASELINE_KEY,)
            )
            result = await row.fetchone()
        if result:
            val = result[0]
            return val if isinstance(val, dict) else json.loads(val)
    except Exception as e:
        logger.warning("signal_health.baseline_load_failed", error=str(e))
    return None


async def _save_baseline(pool, stats: dict) -> None:
    """Persist baseline metrics to bot_state."""
    try:
        async with pool.connection() as conn:
            await conn.execute(
                """INSERT INTO bot_state (key, value, updated_at)
                   VALUES (%s, %s::jsonb, NOW())
                   ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value, updated_at = NOW()""",
                (BASELINE_KEY, json.dumps(stats)),
            )
    except Exception as e:
        logger.error("signal_health.baseline_save_failed", error=str(e))


async def _compute_live_stats(pool) -> dict:
    """Compute live signal stats from DB trades (last 30 days)."""
    try:
        async with pool.connection() as conn:
            rows = await conn.execute(
                """SELECT pnl, pnl_pct
                   FROM trades
                   WHERE timestamp > NOW() - INTERVAL '30 days'
                   ORDER BY timestamp ASC"""
            )
            result = await rows.fetchall()

        if not result:
            return {"ic": 0, "win_rate": 0, "sharpe": 0, "n_trades": 0}

        pnl_pcts = [float(r[1]) for r in result]
        wins = sum(1 for r in result if float(r[0]) > 0)
        n = len(result)
        win_rate = wins / n if n > 0 else 0

        mean_ret = sum(pnl_pcts) / n if n > 0 else 0
        if n > 1:
            variance = sum((x - mean_ret) ** 2 for x in pnl_pcts) / (n - 1)
            std_ret = variance ** 0.5
        else:
            std_ret = 1e-9
        sharpe = mean_ret / std_ret if std_ret > 1e-9 else 0

        # IC approximation: correlation between trade sequence and return
        # (full IC needs raw signal values; this is a proxy using trade index)
        seq = list(range(n))
        ic = compute_signal_ic(seq, pnl_pcts) if n >= 10 else 0

        return {"ic": ic, "win_rate": win_rate, "sharpe": round(sharpe, 4), "n_trades": n}
    except Exception as e:
        logger.error("signal_health.compute_failed", error=str(e))
        return {"ic": 0, "win_rate": 0, "sharpe": 0, "n_trades": 0}


async def run_signal_health_check(pool) -> list[str]:
    """Run a full signal health check: compute live stats, compare to baseline.

    On first run, stores current stats as baseline and returns no alerts.
    """
    live_stats = await _compute_live_stats(pool)
    if live_stats["n_trades"] < 20:
        return []

    baseline = await _get_baseline(pool)
    if baseline is None:
        await _save_baseline(pool, live_stats)
        logger.info("signal_health.baseline_set", stats=live_stats)
        return []

    alerts = check_signal_health(live_stats, baseline)

    # Update baseline monthly (every ~720 hours = 30 days)
    baseline_age = baseline.get("updated_at", 0)
    if time.time() - baseline_age > 30 * 86400:
        live_stats["updated_at"] = time.time()
        await _save_baseline(pool, live_stats)
        logger.info("signal_health.baseline_refreshed", stats=live_stats)

    if alerts:
        logger.warning("signal_health.decay_detected", alerts=alerts)

    return alerts
