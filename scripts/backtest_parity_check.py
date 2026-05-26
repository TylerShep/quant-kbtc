#!/usr/bin/env python3
"""Replay paper trades and score simulator parity."""
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


_REASON_FAMILIES = {
    "HARD_STOP_LOSS": "LOSS_CONTROL",
    "STOP_LOSS": "LOSS_CONTROL",
    "TAKE_PROFIT": "PROFIT_CAPTURE",
    "EXPIRY_GUARD": "SETTLEMENT_GUARD",
    "SHORT_SETTLEMENT_GUARD": "SETTLEMENT_GUARD",
    "CONTRACT_SETTLED": "SETTLEMENT_GUARD",
    "SIGNAL_DECAY": "SIGNAL_QUALITY",
    "HEALTH_SCORE_DECAY": "SIGNAL_QUALITY",
    "VOLATILITY_SPIKE": "SIGNAL_QUALITY",
    "TIME_EXIT": "SIGNAL_QUALITY",
    "STALE_TICKER_CLEANUP": "DATA_SANITY",
    "END_OF_DATA": "DATA_SANITY",
}


def _reason_family(reason: str | None) -> str:
    if not reason:
        return "UNKNOWN"
    return _REASON_FAMILIES.get(reason, "OTHER")


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
    params = [
        symbol,
        *sources,
        _to_dt(start_ts),
        _to_dt(end_ts),
    ]
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


async def _load_paper_trades(
    pool,
    *,
    start_ts: float,
    end_ts: float,
) -> list[dict]:
    # Historical trades have trades.timestamp == trades.closed_at (both = close
    # time, not entry time).  Recover the true entry time from position_telemetry
    # MIN(timestamp) keyed on position_uid.  Trades with no telemetry rows fall
    # back to (closed_at - 300s) as a rough estimate.
    #
    # STALE_TICKER_CLEANUP exits are skipped: they represent positions on
    # already-expired contracts that have no OB ticks after their nominal
    # entry time, so forced-entry replay will never open and always reports
    # NO_SIM_MATCH, inflating the timing_or_state_miss bucket artificially.
    async with pool.connection() as conn:
        rows = await conn.execute(
            """
            SELECT
                t.id,
                t.ticker,
                t.direction,
                t.entry_price,
                t.exit_price,
                t.pnl_pct,
                t.exit_reason,
                EXTRACT(EPOCH FROM t.timestamp)   AS close_ts,
                EXTRACT(EPOCH FROM t.closed_at)   AS exit_ts,
                EXTRACT(EPOCH FROM MIN(pt.timestamp)) AS telemetry_entry_ts,
                t.position_uid
            FROM trades t
            LEFT JOIN position_telemetry pt
                ON pt.position_uid = t.position_uid
               AND pt.trading_mode = 'paper'
            WHERE t.trading_mode = 'paper'
              AND COALESCE(t.closed_at, t.timestamp) >= %s
              AND COALESCE(t.closed_at, t.timestamp) < %s
              AND t.exit_reason != 'STALE_TICKER_CLEANUP'
            GROUP BY t.id, t.ticker, t.direction, t.entry_price, t.exit_price,
                     t.pnl_pct, t.exit_reason, t.timestamp, t.closed_at, t.position_uid
            ORDER BY t.ticker ASC, t.timestamp ASC
            """,
            (_to_dt(start_ts), _to_dt(end_ts)),
        )
        result = await rows.fetchall()
    out = []
    for r in result:
        close_ts = float(r[7])
        exit_ts = float(r[8]) if r[8] is not None else close_ts
        telemetry_entry_ts = float(r[9]) if r[9] is not None else None
        # Prefer telemetry MIN timestamp (real open time).  Fall back to
        # closed_at - 300s only when telemetry is missing entirely.
        entry_ts = telemetry_entry_ts if telemetry_entry_ts is not None else (close_ts - 300.0)
        # Ensure exit_ts >= entry_ts (guard against bad data).
        exit_ts = max(exit_ts, entry_ts)
        out.append(
            {
                "id": int(r[0]),
                "ticker": r[1],
                "direction": r[2],
                "entry_price": float(r[3]),
                "exit_price": float(r[4]) if r[4] is not None else None,
                "pnl_pct": float(r[5]),
                "exit_reason": r[6],
                "entry_ts": entry_ts,
                "exit_ts": exit_ts,
                "position_uid": r[10],
            }
        )
    return out


def _score_match(
    actual: dict,
    sim: dict | None,
    *,
    entry_tolerance_cents: float,
    pnl_tolerance: float,
    exit_price_tolerance_cents: float,
    exit_ts_tolerance_sec: float,
) -> dict:
    if sim is None:
        return {
            "trade_id": actual["id"],
            "ticker": actual["ticker"],
            "matched": False,
            "pass": False,
            "reason": "NO_SIM_MATCH",
        }

    entry_diff = abs(float(sim["entry_price"]) - actual["entry_price"])
    reason_match = sim["exit_reason"] == actual["exit_reason"]
    actual_reason_family = _reason_family(actual.get("exit_reason"))
    sim_reason_family = _reason_family(sim.get("exit_reason"))
    reason_family_match = actual_reason_family == sim_reason_family
    pnl_pct_diff = abs(float(sim["pnl_pct"]) - actual["pnl_pct"])
    actual_exit_price = actual.get("exit_price")
    sim_exit_price = sim.get("exit_price")
    if (
        isinstance(actual_exit_price, (int, float))
        and isinstance(sim_exit_price, (int, float))
    ):
        exit_price_diff = abs(float(sim_exit_price) - float(actual_exit_price))
    else:
        exit_price_diff = None
    exit_ts_diff = abs(float(sim["exit_timestamp"]) - float(actual["exit_ts"]))
    exit_price_ok = (
        exit_price_diff is None
        or exit_price_diff <= exit_price_tolerance_cents
    )
    passed = (
        entry_diff <= entry_tolerance_cents
        and reason_match
        and reason_family_match
        and pnl_pct_diff <= pnl_tolerance
        and exit_price_ok
        and exit_ts_diff <= exit_ts_tolerance_sec
    )
    return {
        "trade_id": actual["id"],
        "ticker": actual["ticker"],
        "matched": True,
        "pass": passed,
        "entry_diff_cents": round(entry_diff, 4),
        "pnl_pct_diff": round(pnl_pct_diff, 6),
        "exit_price_diff_cents": round(exit_price_diff, 4) if exit_price_diff is not None else None,
        "exit_ts_diff_sec": round(exit_ts_diff, 3),
        "actual_exit_reason": actual["exit_reason"],
        "sim_exit_reason": sim["exit_reason"],
        "actual_reason_family": actual_reason_family,
        "sim_reason_family": sim_reason_family,
        "reason_family_match": reason_family_match,
        "actual_entry_ts": actual["entry_ts"],
        "sim_entry_ts": sim["timestamp"],
        "actual_exit_ts": actual["exit_ts"],
        "sim_exit_ts": sim["exit_timestamp"],
        "actual_entry_price": actual["entry_price"],
        "sim_entry_price": sim["entry_price"],
        "actual_exit_price": actual.get("exit_price"),
        "sim_exit_price": sim.get("exit_price"),
        "actual_pnl_pct": actual["pnl_pct"],
        "sim_pnl_pct": sim["pnl_pct"],
    }


def _bucket_mismatch(
    row: dict,
    *,
    entry_tolerance_cents: float,
    pnl_tolerance: float,
    exit_price_tolerance_cents: float,
    exit_ts_tolerance_sec: float,
) -> str:
    if not row.get("matched"):
        reason = row.get("reason")
        if reason in {"NO_TIMELINE_DATA", "NO_CANDLE_DATA"}:
            return "data_availability"
        if reason == "NO_SIM_MATCH":
            return "timing_or_state_miss"
        return "other_unmatched"

    if row.get("pass"):
        return "pass"
    if not row.get("reason_family_match", True):
        return "reason_family_drift"
    if float(row.get("entry_diff_cents", 0.0)) > entry_tolerance_cents:
        return "entry_drift"
    if float(row.get("exit_ts_diff_sec", 0.0)) > exit_ts_tolerance_sec:
        return "timing_drift"
    exit_price_diff = row.get("exit_price_diff_cents")
    if (
        isinstance(exit_price_diff, (int, float))
        and float(exit_price_diff) > exit_price_tolerance_cents
    ):
        return "fill_model_drift"
    if row.get("actual_exit_reason") != row.get("sim_exit_reason"):
        return "reason_order_drift"
    if float(row.get("pnl_pct_diff", 0.0)) > pnl_tolerance:
        return "pnl_residual_drift"
    return "other"


async def run(args) -> dict[str, Any]:
    start_ts = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc).timestamp()
    end_ts = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc).timestamp()
    lookback_sec = max(0, int(args.lookback_minutes * 60))

    pool = await get_pool()
    try:
        trades = await _load_paper_trades(pool, start_ts=start_ts, end_ts=end_ts)
        if not trades:
            return {
                "start": args.start,
                "end": args.end,
                "total_trades": 0,
                "matched_trades": 0,
                "passed_trades": 0,
                "parity_pct": 0.0,
                "details": [],
            }

        global_start = min(t["entry_ts"] for t in trades) - lookback_sec
        global_end = max(t["exit_ts"] for t in trades) + args.replay_tail_seconds
        candles = await _load_candles_window(
            pool,
            start_ts=global_start,
            end_ts=global_end,
            symbol=args.symbol,
            source=args.source,
        )
        timelines = await load_contract_timelines_db(
            pool,
            start_ts=global_start,
            end_ts=global_end,
            tickers=sorted({t["ticker"] for t in trades}),
            series=args.series,
        )
        settlements = await load_settlement_outcomes_db(
            pool,
            tickers=sorted({t["ticker"] for t in trades}),
            start_ts=global_start - 86400.0,
            end_ts=global_end + 86400.0,
            series=args.series,
        )
        for ticker, meta in settlements.items():
            timeline = timelines.get(ticker)
            if timeline is None:
                continue
            timeline.close_time = meta.get("close_time")
            timeline.result = meta.get("result")
            timeline.expiration_value = meta.get("expiration_value")

        details: list[dict] = []
        for trade in trades:
            ticker = trade["ticker"]
            timeline = timelines.get(ticker)
            if timeline is None:
                details.append(
                    {
                        "trade_id": trade["id"],
                        "ticker": ticker,
                        "matched": False,
                        "pass": False,
                        "reason": "NO_TIMELINE_DATA",
                    }
                )
                continue

            ticker_start = trade["entry_ts"] - lookback_sec
            ticker_end = max(trade["exit_ts"], trade["entry_ts"]) + args.replay_tail_seconds
            # Use all global candles from the lookback start onward (no upper
            # bound per-trade).  The backtester computes its internal end_ts as
            # last_candle.timestamp + 900; capping candles at ticker_end would
            # truncate that window before the first OB tick when a BTC candle
            # gap exists between the lookback period and the entry time, causing
            # NO_SIM_MATCH.  ATR/ROC seeding from a broader baseline is correct.
            ticker_candles = [c for c in candles if c["timestamp"] >= ticker_start]
            if not ticker_candles:
                details.append(
                    {
                        "trade_id": trade["id"],
                        "ticker": ticker,
                        "matched": False,
                        "pass": False,
                        "reason": "NO_CANDLE_DATA",
                    }
                )
                continue

            bt = ContractBacktester(
                candles=ticker_candles,
                contract_timelines={ticker: timeline},
                config={
                    "mode": "paper",
                    "hard_stop_loss_pct": args.hard_stop_loss_pct,
                    "paper_same_side_cooldown_sec": args.paper_same_side_cooldown_sec,
                    "health_score_threshold": args.health_score_threshold,
                    "ml_gate_mode": args.ml_gate_mode,
                    "exit_fill_mode": args.exit_fill_mode,
                    "disable_signal_entries": True,
                    "forced_entry": {
                        "ticker": ticker,
                        # Use the true entry timestamp recovered from
                        # position_telemetry so the sim opens the position at
                        # the right point in the price timeline.
                        # allow_open_on_first_tick is intentionally False:
                        # we want the sim to wait for a tick at or after the
                        # real entry time, not open prematurely on a lookback
                        # tick that may carry a stale near-zero price.
                        "entry_ts": trade["entry_ts"],
                        "entry_price": trade["entry_price"],
                        "direction": trade["direction"],
                        "contracts": 1,
                        "allow_open_on_first_tick": False,
                        "signal_driver": "PARITY_REPLAY",
                    },
                },
                settlement_data={ticker: settlements.get(ticker, {})},
            )
            bt.run(bankroll=args.bankroll)
            sim_trade = bt.trades[0] if bt.trades else None
            details.append(
                _score_match(
                    trade,
                    sim_trade,
                    entry_tolerance_cents=args.entry_tolerance_cents,
                    pnl_tolerance=args.pnl_tolerance,
                    exit_price_tolerance_cents=args.exit_price_tolerance_cents,
                    exit_ts_tolerance_sec=args.exit_ts_tolerance_sec,
                )
            )

        for row in details:
            row["mismatch_bucket"] = _bucket_mismatch(
                row,
                entry_tolerance_cents=args.entry_tolerance_cents,
                pnl_tolerance=args.pnl_tolerance,
                exit_price_tolerance_cents=args.exit_price_tolerance_cents,
                exit_ts_tolerance_sec=args.exit_ts_tolerance_sec,
            )

        matched = [d for d in details if d.get("matched")]
        passed = [d for d in matched if d.get("pass")]
        failed = [d for d in details if not d.get("pass")]
        bucket_counts = Counter(d.get("mismatch_bucket", "other") for d in failed)
        attributable = sum(
            count
            for bucket, count in bucket_counts.items()
            if not bucket.startswith("other")
        )
        parity_pct = (len(passed) / len(trades)) if trades else 0.0
        return {
            "start": args.start,
            "end": args.end,
            "total_trades": len(trades),
            "matched_trades": len(matched),
            "passed_trades": len(passed),
            "parity_pct": round(parity_pct, 6),
            "target_parity_pct": args.target_parity,
            "assumptions": {
                "ml_gate_mode": args.ml_gate_mode,
                "exit_fill_mode": args.exit_fill_mode,
            },
            "mismatch_decomposition": {
                "failed_count": len(failed),
                "bucket_counts": dict(bucket_counts),
                "bucket_coverage": round(
                    (attributable / len(failed)) if failed else 1.0,
                    6,
                ),
            },
            "details": details,
        }
    finally:
        await close_pool()


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-trade parity replay")
    parser.add_argument("--start", default="2026-05-05", help="UTC date (YYYY-MM-DD)")
    parser.add_argument("--end", default="2026-05-07", help="UTC date (YYYY-MM-DD, exclusive)")
    parser.add_argument("--lookback-minutes", type=int, default=180)
    parser.add_argument("--symbol", default="BTC")
    parser.add_argument("--series", default="KXBTC")
    parser.add_argument("--source", default="live_spot,binance")
    parser.add_argument("--bankroll", type=float, default=10000.0)
    parser.add_argument("--hard-stop-loss-pct", type=float, default=0.0)
    parser.add_argument("--paper-same-side-cooldown-sec", type=float, default=60.0)
    parser.add_argument("--health-score-threshold", type=float, default=35.0)
    parser.add_argument("--entry-tolerance-cents", type=float, default=1.0)
    parser.add_argument("--exit-price-tolerance-cents", type=float, default=2.0)
    parser.add_argument("--exit-ts-tolerance-sec", type=float, default=90.0)
    parser.add_argument("--pnl-tolerance", type=float, default=0.05)
    parser.add_argument("--replay-tail-seconds", type=float, default=900.0)
    parser.add_argument(
        "--ml-gate-mode",
        choices=["disabled", "config"],
        default="disabled",
        help="ContractBacktester ML gate mode for replay.",
    )
    parser.add_argument(
        "--exit-fill-mode",
        choices=["mark", "executable"],
        default="mark",
        help="Exit fill mode used during replay simulation.",
    )
    parser.add_argument("--target-parity", type=float, default=0.90)
    parser.add_argument(
        "--output",
        default="backtest_reports/parity_check_latest.json",
    )
    args = parser.parse_args()

    result = asyncio.run(run(args))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))

    total = result["total_trades"]
    matched = result["matched_trades"]
    passed = result["passed_trades"]
    parity = result["parity_pct"]
    print(
        f"parity: {passed}/{total} ({parity:.1%}) "
        f"matched={matched}/{total} "
        f"target={args.target_parity:.1%}"
    )

    failures = [d for d in result["details"] if not d.get("pass")]
    if failures:
        print("Top mismatches:")
        for row in failures[:10]:
            print(
                f"  trade_id={row.get('trade_id')} ticker={row.get('ticker')} "
                f"reason={row.get('reason', row.get('sim_exit_reason'))} "
                f"bucket={row.get('mismatch_bucket')} "
                f"entry_diff={row.get('entry_diff_cents')} "
                f"exit_price_diff={row.get('exit_price_diff_cents')} "
                f"exit_ts_diff={row.get('exit_ts_diff_sec')} "
                f"pnl_diff={row.get('pnl_pct_diff')}"
            )

    return 0 if parity >= args.target_parity else 1


if __name__ == "__main__":
    raise SystemExit(main())
