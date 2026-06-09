#!/usr/bin/env python3
"""Sweep STOP_LOSS_PCT and measure impact on paper OBI-long performance.

Rationale: analysis of May 7-11 paper trades shows STOP_LOSS exits are 0% win
rate (-$1,180 total) while EXPIRY_GUARD exits are 69.6% win rate (+$835) and
TAKE_PROFIT exits are 79.5% (+$410). The 2% stop is too tight for binary
contracts that naturally oscillate 10-30% intraday before resolving at expiry.

This sweep tests stop_loss_pct values [0.02, 0.10, 0.20, 0.30, 0.40, 0.50]
against the contract-price backtester to find the threshold where exit-reason
mix shifts from STOP_LOSS-dominant back to EXPIRY_GUARD-dominant.

Also replays actual paper STOP_LOSS trades (May 7-11) without the stop to
measure counterfactual: would those positions have profited at expiry?

Settlement synthesis: kalshi_markets only covers pre-Apr 2026. For Apr-May 2026
contracts, outcomes are derived from BTC candle close price vs ticker strike.

Usage:
    DATABASE_URL=... python3 scripts/backtest_stop_loss_sweep.py \
        --start 2026-05-02 --end 2026-05-12 \
        --output backtest_reports/stop_loss_sweep.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
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

try:
    from zoneinfo import ZoneInfo
    _ET_TZ = ZoneInfo("America/New_York")
except Exception:
    _ET_TZ = timezone(timedelta(hours=-5))

# ── Settlement synthesis helpers ──────────────────────────────────────────────

_TICKER_FULL_RE = re.compile(
    r"^KX[A-Z]+-(\d{2})([A-Z]{3})(\d{2})(\d{2})-([BT])(\d+(?:\.\d+)?)$",
    re.IGNORECASE,
)
_MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_ticker(ticker: str):
    """Return (close_time_utc, direction, strike) or None."""
    m = _TICKER_FULL_RE.match((ticker or "").upper())
    if not m:
        return None
    yy, mmm, dd, hh, direction, strike_str = m.groups()
    month = _MONTH_MAP.get(mmm)
    if month is None:
        return None
    try:
        et_dt = datetime(
            year=2000 + int(yy),
            month=month,
            day=int(dd),
            hour=int(hh),
            minute=0, second=0,
            tzinfo=_ET_TZ,
        )
    except ValueError:
        return None
    close_ts = et_dt.astimezone(timezone.utc).timestamp()
    return close_ts, direction.upper(), float(strike_str)


def _btc_price_at(candles: list[dict], target_ts: float, window_sec: float = 900.0):
    """Return BTC close from candle closest to target_ts within window_sec."""
    best = None
    for c in candles:
        dt = abs(c["timestamp"] - target_ts)
        if dt <= window_sec and (best is None or dt < best[0]):
            best = (dt, c["close"])
    return best[1] if best is not None else None


def _synthesize_settlements(timelines: dict, candles: list[dict]) -> dict[str, dict]:
    """Build settlement_data for tickers absent from kalshi_markets."""
    result: dict[str, dict] = {}
    for ticker in timelines:
        parsed = _parse_ticker(ticker)
        if parsed is None:
            continue
        close_ts, direction, strike = parsed
        btc_price = _btc_price_at(candles, close_ts)
        if btc_price is None:
            continue
        is_yes = btc_price >= strike if direction == "B" else btc_price <= strike
        result[ticker] = {
            "close_time": close_ts,
            "result": "yes" if is_yes else "no",
            "expiration_value": 100.0 if is_yes else 0.0,
        }
    return result


def _to_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _parse_date(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


async def _load_candles(pool, *, start_ts: float, end_ts: float) -> list[dict]:
    query = """
        SELECT DISTINCT ON (timestamp)
               timestamp, open, high, low, close, volume
        FROM candles
        WHERE symbol = %s
          AND source IN (%s, %s)
          AND timestamp >= %s
          AND timestamp <= %s
        ORDER BY timestamp ASC,
                 CASE WHEN source = 'live_spot' THEN 0 ELSE 1 END
    """
    async with pool.connection() as conn:
        rows = await conn.execute(
            query,
            ("BTC", "live_spot", "binance", _to_dt(start_ts), _to_dt(end_ts)),
        )
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


async def _load_paper_stop_trades(
    pool, *, start_ts: float, end_ts: float
) -> list[dict]:
    """Load actual paper STOP_LOSS exits for counterfactual replay."""
    async with pool.connection() as conn:
        rows = await conn.execute(
            """
            SELECT
                id, ticker, direction, entry_price, exit_price, pnl, pnl_pct,
                signal_driver, exit_reason,
                EXTRACT(EPOCH FROM timestamp) AS entry_ts,
                EXTRACT(EPOCH FROM closed_at) AS exit_ts
            FROM trades
            WHERE trading_mode = 'paper'
              AND exit_reason = 'STOP_LOSS'
              AND direction = 'long'
              AND signal_driver LIKE 'OBI%%'
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
            "exit_price": float(r[4]) if r[4] is not None else None,
            "pnl": float(r[5]) if r[5] is not None else None,
            "pnl_pct": float(r[6]) if r[6] is not None else None,
            "signal_driver": r[7],
            "exit_reason": r[8],
            "entry_ts": float(r[9]),
            "exit_ts": float(r[10]) if r[10] is not None else float(r[9]),
        }
        for r in result
    ]


def _summarize(stop_pct: float, result: dict, trades: list[dict]) -> dict:
    long_trades = [t for t in trades if t.get("direction") == "long"]
    total = len(long_trades)
    total_pnl = sum(t.get("pnl", 0.0) for t in long_trades)
    wins = sum(1 for t in long_trades if t.get("pnl", 0.0) > 0)
    win_rate = wins / total if total else 0.0
    avg_pnl = total_pnl / total if total else 0.0

    by_exit = Counter(t.get("exit_reason", "UNKNOWN") for t in long_trades)

    # PnL contribution by exit reason.
    pnl_by_exit: dict[str, float] = {}
    for t in long_trades:
        reason = t.get("exit_reason", "UNKNOWN")
        pnl_by_exit[reason] = round(pnl_by_exit.get(reason, 0.0) + t.get("pnl", 0.0), 2)

    return {
        "stop_loss_pct": stop_pct,
        "total_long_trades": total,
        "win_rate_pct": round(100.0 * win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(avg_pnl, 4),
        "max_drawdown_pct": round(float(result.get("max_drawdown_pct", 0.0)), 2),
        "sharpe_ratio": round(float(result.get("sharpe_ratio", 0.0)), 3),
        "profit_factor": round(float(result.get("profit_factor", 0.0)), 3),
        "exit_reason_counts": dict(by_exit),
        "exit_reason_pnl": pnl_by_exit,
    }


async def run(args) -> dict[str, Any]:
    start_ts = _parse_date(args.start)
    end_ts = _parse_date(args.end)
    cf_start_ts = _parse_date(args.cf_start)
    cf_end_ts = _parse_date(args.cf_end)
    lookback_start = start_ts - 3 * 3600.0

    print(f"Loading candles and contract timelines for {args.start} → {args.end} …")
    pool = await get_pool()
    try:
        candles = await _load_candles(
            pool, start_ts=lookback_start, end_ts=end_ts + 900.0
        )
        timelines = await load_contract_timelines_db(
            pool,
            start_ts=lookback_start,
            end_ts=end_ts + 900.0,
            series=args.series,
            bucket_sec=args.bucket_sec,
        )
        settlements = await load_settlement_outcomes_db(
            pool,
            start_ts=lookback_start - 86400.0,
            end_ts=end_ts + 86400.0,
            series=args.series,
        )
        stop_trades = await _load_paper_stop_trades(
            pool, start_ts=cf_start_ts, end_ts=cf_end_ts
        )
    finally:
        await close_pool()

    # Merge DB settlements into timelines.
    for ticker, meta in settlements.items():
        tl = timelines.get(ticker)
        if tl is None:
            continue
        tl.close_time = meta.get("close_time")
        tl.result = meta.get("result")
        tl.expiration_value = meta.get("expiration_value")

    # Synthesise outcomes for Apr-May 2026 contracts missing from kalshi_markets.
    missing = [t for t in timelines if t not in settlements]
    synthesized = _synthesize_settlements({t: timelines[t] for t in missing}, candles)
    settlements.update(synthesized)
    for ticker, meta in synthesized.items():
        tl = timelines.get(ticker)
        if tl is None:
            continue
        tl.result = meta.get("result")
        tl.expiration_value = meta.get("expiration_value")

    print(
        f"  {len(candles)} candles, {len(timelines)} timelines, "
        f"{len(settlements)} settlements "
        f"({len(synthesized)} synthesised, "
        f"{len(settlements) - len(synthesized)} from kalshi_markets)."
    )
    print(f"  {len(stop_trades)} actual STOP_LOSS long trades for counterfactual "
          f"({args.cf_start} → {args.cf_end}).")

    # Base config mirrors production.
    base_cfg = {
        "mode": "paper",
        "hard_stop_loss_pct": 0.0,
        # Effectively long-only: set short_threshold to 0 so OBI shorts never fire.
        "short_threshold": 0.0,
        "paper_same_side_cooldown_sec": args.paper_cooldown_sec,
        "paper_thesis_flip_unlock_enabled": True,
        "health_score_threshold": args.health_score_threshold,
        "health_score_breach_ticks": args.health_score_breach_ticks,
        # Shadow Exit Intelligence by default: at 15s OB bucket resolution,
        # 3 breach_ticks = 45s vs sub-second in production, causing severe
        # EI over-firing that masks the stop-loss signal. Use --no-shadow-ei
        # to restore EI for comparison.
        "exit_intelligence_enabled": True,
        "exit_intelligence_shadow_only": not args.no_shadow_ei,
        "health_exit_confirmation_enabled": True,
        "health_exit_confirmation_roc_delta": 0.05,
        "health_exit_confirmation_obi_delta": 0.05,
        "health_exit_confirmation_neutral_obi": 0.50,
        "exit_fill_mode": "mark",
        "ml_gate_mode": "disabled",
        # Block hours 14 and 17 UTC (8am and 11am CT, both deployed in production).
        "blocked_hours_utc": [14, 17],
        # OBI long threshold updated to 0.68 (deployed 2026-05-26).
        "long_threshold": 0.68,
    }

    stop_values = [round(v, 2) for v in args.stop_values]
    print(f"\nSweeping stop_loss_pct values: {stop_values}")
    print(f"{'stop_pct':>9}  {'n':>5}  {'WR%':>6}  {'avg_pnl':>8}  {'total_pnl':>10}  "
          f"{'STOP_LOSS':>9}  {'EXPIRY':>7}  {'TAKE_P':>7}")

    sweeps: list[dict] = []
    for stop_pct in stop_values:
        cfg = {**base_cfg, "stop_loss_pct": stop_pct}
        bt = ContractBacktester(
            candles=candles,
            contract_timelines=timelines,
            config=cfg,
            settlement_data=settlements,
        )
        result = bt.run(bankroll=args.bankroll)
        summary = _summarize(stop_pct, result, bt.trades)
        sweeps.append(summary)

        n_sl = summary["exit_reason_counts"].get("STOP_LOSS", 0)
        n_eg = summary["exit_reason_counts"].get("EXPIRY_GUARD", 0)
        n_tp = summary["exit_reason_counts"].get("TAKE_PROFIT", 0)
        print(
            f"  sl={stop_pct:.2f}  "
            f"n={summary['total_long_trades']:5d}  "
            f"WR={summary['win_rate_pct']:5.1f}%  "
            f"avg=${summary['avg_pnl']:7.2f}  "
            f"pnl=${summary['total_pnl']:9,.2f}  "
            f"SL={n_sl:4d}  EG={n_eg:4d}  TP={n_tp:4d}"
        )

    # ── Counterfactual: replay actual STOP_LOSS paper trades without the stop ──
    print(f"\nCounterfactual: replaying {len(stop_trades)} actual STOP_LOSS trades "
          "with stop disabled…")
    lookback_sec = 3 * 3600.0
    cf_rows: list[dict] = []
    for row in stop_trades:
        ticker = row["ticker"]
        timeline = timelines.get(ticker)
        if timeline is None:
            cf_rows.append({"trade_id": row["id"], "ticker": ticker,
                            "matched": False, "reason": "NO_TIMELINE"})
            continue

        t_start = row["entry_ts"] - lookback_sec
        t_end = max(row["exit_ts"], row["entry_ts"]) + 900.0
        ticker_candles = [c for c in candles if t_start <= c["timestamp"] <= t_end]
        if not ticker_candles:
            cf_rows.append({"trade_id": row["id"], "ticker": ticker,
                            "matched": False, "reason": "NO_CANDLES"})
            continue

        bt = ContractBacktester(
            candles=ticker_candles,
            contract_timelines={ticker: timeline},
            config={
                **base_cfg,
                # Disable the stop entirely for this counterfactual.
                "stop_loss_pct": 999.0,
                "hard_stop_loss_pct": 0.0,
                "disable_signal_entries": True,
                "forced_entry": {
                    "ticker": ticker,
                    "entry_ts": row["entry_ts"],
                    "entry_price": row["entry_price"],
                    "direction": row["direction"],
                    "contracts": 1,
                    "allow_open_on_first_tick": True,
                    "signal_driver": "STOP_LOSS_COUNTERFACTUAL",
                },
            },
            settlement_data={ticker: settlements.get(ticker, {})},
        )
        bt.run(bankroll=args.bankroll)
        sim = bt.trades[0] if bt.trades else None
        if sim is None:
            cf_rows.append({"trade_id": row["id"], "ticker": ticker,
                            "matched": False, "reason": "NO_SIM_TRADE"})
            continue

        cf_rows.append({
            "trade_id": row["id"],
            "ticker": ticker,
            "matched": True,
            "actual_exit": row["exit_reason"],
            "actual_pnl": row["pnl"],
            "actual_pnl_pct": row["pnl_pct"],
            "actual_entry_price": row["entry_price"],
            "actual_exit_price": row["exit_price"],
            "cf_exit": sim.get("exit_reason"),
            "cf_pnl": sim.get("pnl"),
            "cf_pnl_pct": sim.get("pnl_pct"),
            "cf_mfe": sim.get("max_favorable_excursion"),
            "cf_mae": sim.get("max_adverse_excursion"),
        })

    matched = [r for r in cf_rows if r.get("matched")]
    cf_pnl_total = sum(r.get("cf_pnl", 0.0) or 0.0 for r in matched)
    actual_pnl_total = sum(r.get("actual_pnl", 0.0) or 0.0 for r in matched)
    cf_wins = sum(1 for r in matched if (r.get("cf_pnl") or 0.0) > 0)
    cf_exit_dist = Counter(r.get("cf_exit") for r in matched)

    print(f"  Matched {len(matched)}/{len(stop_trades)} trades for counterfactual.")
    if matched:
        print(f"  Actual STOP_LOSS PnL:           ${actual_pnl_total:+,.2f}")
        print(f"  Counterfactual (no stop) PnL:   ${cf_pnl_total:+,.2f}")
        print(f"  Counterfactual win rate:        {100.0 * cf_wins / len(matched):.1f}%")
        print(f"  Counterfactual exit distribution: {dict(cf_exit_dist)}")
        print(f"  Delta (hold vs stop):           ${cf_pnl_total - actual_pnl_total:+,.2f}")

    output = {
        "window": {"start": args.start, "end": args.end},
        "counterfactual_window": {"start": args.cf_start, "end": args.cf_end},
        "base_config": {k: v for k, v in base_cfg.items()
                        if k not in ("stop_loss_pct",)},
        "current_production_stop_loss_pct": 0.02,
        "sweeps": sweeps,
        "ranked_by_avg_pnl": sorted(
            [{"rank": 0, "stop_loss_pct": s["stop_loss_pct"],
              "win_rate_pct": s["win_rate_pct"],
              "avg_pnl": s["avg_pnl"],
              "total_pnl": s["total_pnl"],
              "total_trades": s["total_long_trades"],
              "stop_loss_exits": s["exit_reason_counts"].get("STOP_LOSS", 0),
              "expiry_guard_exits": s["exit_reason_counts"].get("EXPIRY_GUARD", 0),
              }
             for s in sweeps],
            key=lambda r: r["avg_pnl"],
            reverse=True,
        ),
        "counterfactual": {
            "total_actual_stop_trades": len(stop_trades),
            "matched": len(matched),
            "actual_pnl_total": round(actual_pnl_total, 2),
            "cf_pnl_total": round(cf_pnl_total, 2),
            "delta": round(cf_pnl_total - actual_pnl_total, 2),
            "cf_win_rate_pct": round(100.0 * cf_wins / len(matched), 1) if matched else 0.0,
            "cf_exit_distribution": dict(cf_exit_dist),
            "rows": cf_rows,
        },
    }

    # Add ranks.
    for i, row in enumerate(output["ranked_by_avg_pnl"]):
        row["rank"] = i + 1

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2))

    print(f"\n=== Ranked by avg_pnl ===")
    print(f"{'Rank':>4}  {'sl_pct':>6}  {'WR%':>6}  {'avg_pnl':>8}  "
          f"{'total_pnl':>10}  {'trades':>6}  {'SL_exits':>8}  {'EG_exits':>8}")
    for row in output["ranked_by_avg_pnl"]:
        marker = "  ← current" if row["stop_loss_pct"] == 0.02 else ""
        print(
            f"{row['rank']:>4}  {row['stop_loss_pct']:>6.2f}  "
            f"{row['win_rate_pct']:>6.1f}  {row['avg_pnl']:>8.2f}  "
            f"{row['total_pnl']:>10,.2f}  {row['total_trades']:>6}  "
            f"{row['stop_loss_exits']:>8}  {row['expiry_guard_exits']:>8}{marker}"
        )

    print(f"\nFull results written to {out_path}")
    return output


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-05-02",
                        help="Backtest window start (YYYY-MM-DD)")
    parser.add_argument("--end", default="2026-05-12",
                        help="Backtest window end (exclusive, YYYY-MM-DD)")
    parser.add_argument("--cf-start", default="2026-05-07",
                        help="Counterfactual window start (actual STOP_LOSS trades)")
    parser.add_argument("--cf-end", default="2026-05-12",
                        help="Counterfactual window end")
    parser.add_argument("--series", default="KXBTC")
    parser.add_argument(
        "--stop-values",
        nargs="+",
        type=float,
        default=[0.02, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 999.0],
        dest="stop_values",
    )
    parser.add_argument(
        "--no-shadow-ei",
        action="store_true",
        default=False,
        dest="no_shadow_ei",
        help="Disable EI shadowing (not recommended at 15s bucket resolution).",
    )
    parser.add_argument("--bankroll", type=float, default=10000.0)
    parser.add_argument("--paper-cooldown-sec", type=float, default=60.0,
                        dest="paper_cooldown_sec")
    parser.add_argument("--health-score-threshold", type=float, default=25.0,
                        dest="health_score_threshold")
    parser.add_argument("--health-score-breach-ticks", type=int, default=3,
                        dest="health_score_breach_ticks")
    parser.add_argument(
        "--bucket-sec", type=int, default=0, dest="bucket_sec",
        help="Thin OB snapshots to one per N-second bucket per ticker. "
             "Use 10-30 for multi-week windows to avoid OOM. Default 0 = full resolution.",
    )
    parser.add_argument(
        "--output",
        default="backtest_reports/stop_loss_sweep.json",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    _main()
