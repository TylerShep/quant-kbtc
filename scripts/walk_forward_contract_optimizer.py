#!/usr/bin/env python3
"""Run joint walk-forward optimization for HARD_STOP/cooldown/health."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)

from backtesting.data_loader import load_contract_timelines_db, load_settlement_outcomes_db
from backtesting.walk_forward import WalkForwardOptimizer
from database import close_pool, get_pool
from filters.atr_regime import ATRRegimeFilter


def _to_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _parse_date(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def _majority_vote(param_dicts: list[dict]) -> dict:
    votes: dict[str, Counter] = defaultdict(Counter)
    for params in param_dicts:
        for key, val in params.items():
            votes[key][val] += 1
    return {
        key: counter.most_common(1)[0][0]
        for key, counter in votes.items()
        if counter
    }


def _confidence_metadata(
    *,
    windows: list,
    edge_consistency: float,
    overfitting: dict[str, Any],
) -> dict[str, Any]:
    if not windows:
        return {
            "confidence_score": 0.0,
            "confidence_label": "low",
            "profitable_windows": 0,
            "total_windows": 0,
            "notes": ["No valid windows."],
        }

    profitable_windows = sum(1 for w in windows if w.test_sharpe > 0)
    avg_gap = float(overfitting.get("avg_overfitting_gap", 0.0))
    # Penalize high gap while rewarding consistency.
    gap_penalty = 1.0 / (1.0 + max(0.0, avg_gap))
    confidence_score = max(0.0, min(1.0, edge_consistency * gap_penalty))
    if confidence_score >= 0.60:
        label = "high"
    elif confidence_score >= 0.35:
        label = "medium"
    else:
        label = "low"

    notes = []
    if overfitting.get("high_overfitting"):
        notes.append("High overfitting gap detected.")
    if overfitting.get("inconsistent_edge"):
        notes.append("Edge consistency below desired threshold.")
    if not notes:
        notes.append("Edge and gap diagnostics are within expected range.")

    return {
        "confidence_score": round(confidence_score, 6),
        "confidence_label": label,
        "profitable_windows": profitable_windows,
        "total_windows": len(windows),
        "notes": notes,
    }


async def _load_candles_window(pool, *, start_ts: float, end_ts: float, symbol: str, source: str) -> list[dict]:
    sources = [s.strip() for s in source.split(",") if s.strip()]
    placeholders = ",".join(["%s"] * len(sources))
    params = [symbol, *sources, _to_dt(start_ts), _to_dt(end_ts)]
    query = f"""
        SELECT DISTINCT ON (timestamp)
               timestamp, open, high, low, close, volume
        FROM candles
        WHERE symbol = %s
          AND source IN ({placeholders})
          AND timestamp >= %s
          AND timestamp <= %s
        ORDER BY timestamp ASC,
                 CASE WHEN source = 'live_spot' THEN 0 ELSE 1 END
    """
    async with pool.connection() as conn:
        rows = await conn.execute(query, params)
        result = await rows.fetchall()
    return [
        {
            "timestamp": r[0].timestamp(),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
        }
        for r in result
    ]


def _build_regime_series(candles: list[dict]) -> list[str]:
    atr = ATRRegimeFilter()
    out: list[str] = []
    for c in candles:
        out.append(atr.update(c["high"], c["low"], c["close"]))
    return out


async def run(args) -> dict[str, Any]:
    start_ts = _parse_date(args.start)
    end_ts = _parse_date(args.end)
    param_space = {
        "hard_stop_loss_pct": args.hard_stop_values,
        "paper_same_side_cooldown_sec": args.paper_cooldown_values,
        "health_score_threshold": args.health_threshold_values,
    }

    pool = await get_pool()
    try:
        candles = await _load_candles_window(
            pool,
            start_ts=start_ts,
            end_ts=end_ts,
            symbol=args.symbol,
            source=args.source,
        )
        min_required_candles = 384  # 3 days train + 1 day test on 15m candles
        if len(candles) < min_required_candles:
            return {
                "start": args.start,
                "end": args.end,
                "reason": "insufficient_candles",
                "candle_count": len(candles),
                "required_candle_count": min_required_candles,
            }

        timelines = await load_contract_timelines_db(
            pool,
            start_ts=start_ts,
            end_ts=end_ts + 900.0,
            series=args.series,
        )
        settlements = await load_settlement_outcomes_db(
            pool,
            start_ts=start_ts - 86400.0,
            end_ts=end_ts + 86400.0,
            series=args.series,
        )
        for ticker, meta in settlements.items():
            timeline = timelines.get(ticker)
            if timeline is None:
                continue
            timeline.close_time = meta.get("close_time")
            timeline.result = meta.get("result")
            timeline.expiration_value = meta.get("expiration_value")

        optimizer = WalkForwardOptimizer(
            candles=candles,
            contract_timelines=timelines,
            settlement_data=settlements,
            tfi_history={},
            base_config={
                "mode": "paper",
                "ml_gate_mode": args.ml_gate_mode,
                "exit_fill_mode": args.exit_fill_mode,
            },
        )
        windows = optimizer.run(param_space, objective=args.objective)
        overfitting = optimizer.diagnose_overfitting(windows)
        global_params = optimizer.select_final_params(windows) or {}
        edge_consistency = optimizer.edge_consistency(windows)
        confidence_metadata = _confidence_metadata(
            windows=windows,
            edge_consistency=edge_consistency,
            overfitting=overfitting,
        )

        regime_series = _build_regime_series(candles)
        regime_votes: dict[str, list[dict]] = {"LOW": [], "MEDIUM": [], "HIGH": []}
        per_window: list[dict] = []
        for w in windows:
            test_slice = regime_series[w.test_range[0]: w.test_range[1]]
            dominant = Counter(test_slice).most_common(1)[0][0] if test_slice else "UNKNOWN"
            if dominant in regime_votes and w.test_sharpe > 0:
                regime_votes[dominant].append(w.best_params)
            per_window.append(
                {
                    "window_id": w.window_id,
                    "train_range": list(w.train_range),
                    "test_range": list(w.test_range),
                    "dominant_test_regime": dominant,
                    "best_params": w.best_params,
                    "train_sharpe": w.train_sharpe,
                    "test_sharpe": w.test_sharpe,
                    "test_win_rate": w.test_win_rate,
                    "test_trades": w.test_trades,
                    "overfitting_gap": w.overfitting_gap,
                }
            )

        recommendations_by_regime = {
            regime: _majority_vote(votes) if votes else {}
            for regime, votes in regime_votes.items()
        }

        return {
            "start": args.start,
            "end": args.end,
            "objective": args.objective,
            "param_space": param_space,
            "assumptions": {
                "ml_gate_mode": args.ml_gate_mode,
                "exit_fill_mode": args.exit_fill_mode,
            },
            "window_count": len(windows),
            "edge_consistency": edge_consistency,
            "confidence_metadata": confidence_metadata,
            "global_recommendation": global_params,
            "recommendations_by_regime": recommendations_by_regime,
            "overfitting_diagnosis": overfitting,
            "windows": per_window,
        }
    finally:
        await close_pool()


def main() -> int:
    parser = argparse.ArgumentParser(description="Joint walk-forward optimizer")
    parser.add_argument("--start", default="2026-04-16")
    parser.add_argument("--end", default="2026-05-06")
    parser.add_argument("--symbol", default="BTC")
    parser.add_argument("--series", default="KXBTC")
    parser.add_argument("--source", default="live_spot,binance")
    parser.add_argument("--objective", default="sharpe_ratio")
    parser.add_argument(
        "--hard-stop-values",
        nargs="+",
        type=float,
        default=[0.0, 0.10, 0.30],
        help="Grid values for hard_stop_loss_pct.",
    )
    parser.add_argument(
        "--paper-cooldown-values",
        nargs="+",
        type=float,
        default=[30.0, 60.0],
        help="Grid values for paper_same_side_cooldown_sec.",
    )
    parser.add_argument(
        "--health-threshold-values",
        nargs="+",
        type=float,
        default=[25.0, 35.0],
        help="Grid values for health_score_threshold.",
    )
    parser.add_argument(
        "--ml-gate-mode",
        choices=["disabled", "config"],
        default="disabled",
        help="ContractBacktester ML gate mode for optimization runs.",
    )
    parser.add_argument(
        "--exit-fill-mode",
        choices=["mark", "executable"],
        default="mark",
        help="Exit fill mode used by contract backtester.",
    )
    parser.add_argument("--output", default="backtest_reports/walk_forward_contract_joint.json")
    args = parser.parse_args()

    result = asyncio.run(run(args))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))

    print(f"window_count={result.get('window_count', 0)}")
    print(f"edge_consistency={result.get('edge_consistency', 0):.1%}")
    print(f"global={result.get('global_recommendation', {})}")
    print(f"by_regime={result.get('recommendations_by_regime', {})}")
    print(f"saved={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
