#!/usr/bin/env python3
"""Run HARD_STOP_LOSS sweeps on the contract-price backtester."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)

from backtesting.contract_backtester import ContractBacktester
from backtesting.data_loader import load_contract_timelines_db, load_settlement_outcomes_db
from database import close_pool, get_pool


def _to_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _parse_date(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


async def _load_candles_window(
    pool,
    *,
    start_ts: float,
    end_ts: float,
    symbol: str,
    source: str,
) -> list[dict]:
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


async def _load_hard_stop_trades(
    pool,
    *,
    start_ts: float,
    end_ts: float,
) -> list[dict]:
    async with pool.connection() as conn:
        rows = await conn.execute(
            """
            SELECT
                id,
                ticker,
                direction,
                entry_price,
                pnl_pct,
                exit_reason,
                EXTRACT(EPOCH FROM timestamp) AS entry_ts,
                EXTRACT(EPOCH FROM closed_at) AS exit_ts
            FROM trades
            WHERE trading_mode = 'paper'
              AND exit_reason = 'HARD_STOP_LOSS'
              AND timestamp >= %s
              AND timestamp < %s
            ORDER BY ticker ASC, timestamp ASC
            """,
            (_to_dt(start_ts), _to_dt(end_ts)),
        )
        result = await rows.fetchall()
    return [
        {
            "id": int(r[0]),
            "ticker": r[1],
            "direction": r[2],
            "entry_price": float(r[3]),
            "pnl_pct": float(r[4]),
            "exit_reason": r[5],
            "entry_ts": float(r[6]),
            "exit_ts": float(r[7]) if r[7] is not None else float(r[6]),
        }
        for r in result
    ]


def _summarize_run(hard_stop: float, result: dict, trades: list[dict]) -> dict:
    exit_reasons = Counter(t.get("exit_reason", "UNKNOWN") for t in trades)
    return {
        "hard_stop_loss_pct": hard_stop,
        "total_trades": int(result.get("total_trades", 0)),
        "win_rate": float(result.get("win_rate", 0.0)),
        "total_pnl": float(result.get("total_pnl", 0.0)),
        "max_drawdown_pct": float(result.get("max_drawdown_pct", 0.0)),
        "sharpe_ratio": float(result.get("sharpe_ratio", 0.0)),
        "profit_factor": float(result.get("profit_factor", 0.0)),
        "exit_reasons": dict(exit_reasons),
    }


async def run(args) -> dict[str, Any]:
    start_ts = _parse_date(args.start)
    end_ts = _parse_date(args.end)
    cf_start_ts = _parse_date(args.counterfactual_start)
    cf_end_ts = _parse_date(args.counterfactual_end)
    lookback_sec = float(args.lookback_minutes) * 60.0

    pool = await get_pool()
    try:
        candles = await _load_candles_window(
            pool,
            start_ts=start_ts,
            end_ts=end_ts,
            symbol=args.symbol,
            source=args.source,
        )
        if not candles:
            return {
                "start": args.start,
                "end": args.end,
                "sweeps": [],
                "counterfactual": [],
            }

        timelines = await load_contract_timelines_db(
            pool,
            start_ts=min(start_ts, cf_start_ts - lookback_sec),
            end_ts=max(end_ts, cf_end_ts) + 900.0,
            series=args.series,
        )
        settlements = await load_settlement_outcomes_db(
            pool,
            start_ts=min(start_ts, cf_start_ts) - 86400.0,
            end_ts=max(end_ts, cf_end_ts) + 86400.0,
            series=args.series,
        )
        for ticker, meta in settlements.items():
            timeline = timelines.get(ticker)
            if timeline is None:
                continue
            timeline.close_time = meta.get("close_time")
            timeline.result = meta.get("result")
            timeline.expiration_value = meta.get("expiration_value")

        sweeps: list[dict] = []
        for hard_stop in args.hard_stop_values:
            bt = ContractBacktester(
                candles=candles,
                contract_timelines=timelines,
                config={
                    "mode": "paper",
                    "hard_stop_loss_pct": hard_stop,
                    "paper_same_side_cooldown_sec": args.paper_same_side_cooldown_sec,
                    "health_score_threshold": args.health_score_threshold,
                    "ml_gate_mode": args.ml_gate_mode,
                    "exit_fill_mode": args.exit_fill_mode,
                },
                settlement_data=settlements,
            )
            result = bt.run(bankroll=args.bankroll)
            sweeps.append(_summarize_run(hard_stop, result, bt.trades))

        hard_stop_trades = await _load_hard_stop_trades(
            pool,
            start_ts=cf_start_ts,
            end_ts=cf_end_ts,
        )
        counterfactual_rows: list[dict] = []
        for row in hard_stop_trades:
            ticker = row["ticker"]
            timeline = timelines.get(ticker)
            if timeline is None:
                counterfactual_rows.append(
                    {
                        "trade_id": row["id"],
                        "ticker": ticker,
                        "matched": False,
                        "reason": "NO_TIMELINE_DATA",
                    }
                )
                continue

            ticker_start = row["entry_ts"] - lookback_sec
            ticker_end = max(row["exit_ts"], row["entry_ts"]) + 900.0
            ticker_candles = [
                c for c in candles if ticker_start <= c["timestamp"] <= ticker_end
            ]
            if not ticker_candles:
                counterfactual_rows.append(
                    {
                        "trade_id": row["id"],
                        "ticker": ticker,
                        "matched": False,
                        "reason": "NO_CANDLE_DATA",
                    }
                )
                continue

            bt = ContractBacktester(
                candles=ticker_candles,
                contract_timelines={ticker: timeline},
                config={
                    "mode": "paper",
                    "hard_stop_loss_pct": 0.0,
                    "paper_same_side_cooldown_sec": args.paper_same_side_cooldown_sec,
                    "health_score_threshold": args.health_score_threshold,
                    "ml_gate_mode": args.ml_gate_mode,
                    "exit_fill_mode": args.exit_fill_mode,
                    "disable_signal_entries": True,
                    "forced_entry": {
                        "ticker": ticker,
                        "entry_ts": row["entry_ts"],
                        "entry_price": row["entry_price"],
                        "direction": row["direction"],
                        "contracts": 1,
                        "allow_open_on_first_tick": True,
                        "signal_driver": "HARD_STOP_COUNTERFACTUAL",
                    },
                },
                settlement_data={ticker: settlements.get(ticker, {})},
            )
            bt.run(bankroll=args.bankroll)
            sim = bt.trades[0] if bt.trades else None
            if sim is None:
                counterfactual_rows.append(
                    {
                        "trade_id": row["id"],
                        "ticker": ticker,
                        "matched": False,
                        "reason": "NO_SIM_MATCH",
                    }
                )
                continue
            counterfactual_rows.append(
                {
                    "trade_id": row["id"],
                    "ticker": ticker,
                    "matched": True,
                    "actual_exit_reason": row["exit_reason"],
                    "actual_pnl_pct": row["pnl_pct"],
                    "counterfactual_exit_reason": sim.get("exit_reason"),
                    "counterfactual_pnl_pct": sim.get("pnl_pct"),
                    "counterfactual_mfe": sim.get("max_favorable_excursion"),
                    "counterfactual_mae": sim.get("max_adverse_excursion"),
                }
            )

        return {
            "start": args.start,
            "end": args.end,
            "bankroll": args.bankroll,
            "hard_stop_values": args.hard_stop_values,
            "assumptions": {
                "ml_gate_mode": args.ml_gate_mode,
                "exit_fill_mode": args.exit_fill_mode,
            },
            "sweeps": sweeps,
            "counterfactual_window": {
                "start": args.counterfactual_start,
                "end": args.counterfactual_end,
            },
            "counterfactual": counterfactual_rows,
        }
    finally:
        await close_pool()


def main() -> int:
    parser = argparse.ArgumentParser(description="Contract backtester HARD_STOP sweep")
    parser.add_argument("--start", default="2026-04-16")
    parser.add_argument("--end", default="2026-05-06")
    parser.add_argument("--counterfactual-start", default="2026-05-05")
    parser.add_argument("--counterfactual-end", default="2026-05-07")
    parser.add_argument("--lookback-minutes", type=int, default=180)
    parser.add_argument("--symbol", default="BTC")
    parser.add_argument("--series", default="KXBTC")
    parser.add_argument("--source", default="live_spot,binance")
    parser.add_argument("--bankroll", type=float, default=10000.0)
    parser.add_argument("--paper-same-side-cooldown-sec", type=float, default=60.0)
    parser.add_argument("--health-score-threshold", type=float, default=35.0)
    parser.add_argument(
        "--ml-gate-mode",
        choices=["disabled", "config"],
        default="disabled",
        help="ContractBacktester ML gate mode for sweeps/counterfactual.",
    )
    parser.add_argument(
        "--exit-fill-mode",
        choices=["mark", "executable"],
        default="mark",
        help="Exit fill mode used by contract backtester.",
    )
    parser.add_argument(
        "--hard-stop-values",
        nargs="+",
        type=float,
        default=[0.0, 0.10, 0.20, 0.30, 0.50],
    )
    parser.add_argument(
        "--output",
        default="backtest_reports/hard_stop_sweep_v2.json",
    )
    args = parser.parse_args()

    result = asyncio.run(run(args))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))

    print("HARD_STOP sweep:")
    for row in result["sweeps"]:
        print(
            f"  hs={row['hard_stop_loss_pct']:.2f} "
            f"trades={row['total_trades']} "
            f"wr={row['win_rate']:.1%} "
            f"pnl=${row['total_pnl']:+,.2f} "
            f"dd={row['max_drawdown_pct']:.2f}% "
            f"sharpe={row['sharpe_ratio']:.2f} "
            f"pf={row['profit_factor']:.2f}"
        )
    print(f"Counterfactual rows: {len(result['counterfactual'])}")
    print(f"Saved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
