#!/usr/bin/env python3
"""Sweep OBI_LONG_THRESHOLD and measure impact on paper strategy performance.

Runs the ContractBacktester across a range of OBI long (and mirrored short)
thresholds against the production window, reporting win rate, avg PnL, exit
reason mix, and effective trade count so we can see where the sweet spot is.

Also computes a counterfactual for blocking hour 17 UTC by tagging which
backtest trades were entered in that hour (the strategy is single-position,
so skipping an entry == losing that PnL, no downstream cascade).

Settlement synthesis: the kalshi_markets table only has pre-loaded historical
data through March 2026. For April-May 2026 the script synthesises settlement
outcomes from BTC candle close prices at contract expiry and the strike
embedded in the ticker (B<strike> = YES if BTC >= strike, T<strike> = YES if
BTC <= strike). Positions that cannot be matched to a candle within 15 minutes
of close_time get STALE_TICKER_CLEANUP treatment (zero PnL).

Usage:
    DATABASE_URL=... python3 scripts/backtest_obi_threshold_sweep.py \
        --start 2026-04-16 --end 2026-05-11 \
        --output backtest_reports/obi_threshold_sweep.json
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

# KXBTC-26MAY1109-B81150  →  groups: (yy, mmm, dd, hh, direction, strike)
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
    from datetime import timedelta
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
    """Return BTC close from candle closest to *target_ts* within *window_sec*."""
    best = None
    for c in candles:
        dt = abs(c["timestamp"] - target_ts)
        if dt <= window_sec and (best is None or dt < best[0]):
            best = (dt, c["close"])
    return best[1] if best is not None else None


def _synthesize_settlements(
    timelines: dict,
    candles: list[dict],
) -> dict[str, dict]:
    """Build settlement_data for tickers whose kalshi_markets row is absent.

    Parses close_time and strike from the ticker itself (same ET→UTC logic
    as ContractBacktester._ticker_close_time), then compares to the BTC
    candle close at expiry to determine YES/NO.
    """
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


def _summarize(long_thresh: float, result: dict, trades: list[dict]) -> dict:
    total = int(result.get("total_trades", 0))
    total_pnl = float(result.get("total_pnl", 0.0))
    wins = sum(1 for t in trades if t.get("pnl", 0.0) > 0)
    win_rate = wins / total if total else 0.0
    avg_pnl = total_pnl / total if total else 0.0

    by_exit = Counter(t.get("exit_reason", "UNKNOWN") for t in trades)
    by_driver = Counter(
        f"{t.get('signal_driver','?')}/{t.get('direction','?')}"
        for t in trades
    )

    # Counterfactual: remove hour-17 UTC entries from PnL total.
    h17_trades = [
        t for t in trades
        if _to_dt(t.get("timestamp", 0.0)).hour == 17
    ]
    h17_pnl = sum(t.get("pnl", 0.0) for t in h17_trades)
    pnl_ex_h17 = total_pnl - h17_pnl
    trades_ex_h17 = total - len(h17_trades)

    # Win rate for each OBI bucket within this run.
    obi_buckets: dict[str, dict] = {}
    for lo, hi, label in [
        (0.0,  0.50, "<0.50"),
        (0.50, 0.55, "0.50-0.55"),
        (0.55, 0.60, "0.55-0.60"),
        (0.60, 0.65, "0.60-0.65"),
        (0.65, 0.70, "0.65-0.70"),
        (0.70, 1.01, "0.70+"),
    ]:
        bucket_trades = [
            t for t in trades
            if lo <= t.get("entry_obi", 0.0) < hi
        ]
        if bucket_trades:
            b_pnl = sum(t.get("pnl", 0.0) for t in bucket_trades)
            b_wins = sum(1 for t in bucket_trades if t.get("pnl", 0.0) > 0)
            obi_buckets[label] = {
                "count": len(bucket_trades),
                "pnl": round(b_pnl, 2),
                "win_pct": round(100.0 * b_wins / len(bucket_trades), 1),
            }

    return {
        "long_threshold": long_thresh,
        "total_trades": total,
        "win_rate": round(win_rate, 4),
        "win_rate_pct": round(100.0 * win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(avg_pnl, 4),
        "max_drawdown_pct": round(float(result.get("max_drawdown_pct", 0.0)), 2),
        "sharpe_ratio": round(float(result.get("sharpe_ratio", 0.0)), 3),
        "profit_factor": round(float(result.get("profit_factor", 0.0)), 3),
        "exit_reasons": dict(by_exit),
        "drivers": dict(by_driver),
        "obi_buckets_in_run": obi_buckets,
        "h17_counterfactual": {
            "h17_trades": len(h17_trades),
            "h17_pnl": round(h17_pnl, 2),
            "pnl_ex_h17": round(pnl_ex_h17, 2),
            "trades_ex_h17": trades_ex_h17,
            "wr_ex_h17_pct": round(
                100.0
                * sum(1 for t in trades if t.get("pnl", 0.0) > 0 and _to_dt(t.get("timestamp", 0.0)).hour != 17)
                / trades_ex_h17,
                1,
            )
            if trades_ex_h17
            else 0.0,
        },
    }


async def run(args) -> dict[str, Any]:
    start_ts = _parse_date(args.start)
    end_ts = _parse_date(args.end)
    lookback_start = start_ts - 3 * 3600.0  # ATR warmup buffer

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

    # For tickers not covered by kalshi_markets (e.g. Apr-May 2026 live data),
    # synthesise settlement outcomes from BTC candle prices at close_time.
    missing_tickers = [t for t in timelines if t not in settlements]
    synthesized = _synthesize_settlements(
        {t: timelines[t] for t in missing_tickers},
        candles,
    )
    # Inject synthesized outcomes into settlement_data (passed to backtester).
    settlements.update(synthesized)
    # Also stamp them onto the timelines so _settlement_for gets close_time.
    for ticker, meta in synthesized.items():
        tl = timelines.get(ticker)
        if tl is None:
            continue
        tl.result = meta.get("result")
        tl.expiration_value = meta.get("expiration_value")

    print(
        f"  {len(candles)} candles, {len(timelines)} contract timelines, "
        f"{len(settlements)} settlements "
        f"({len(synthesized)} synthesised from BTC candles, "
        f"{len(settlements) - len(synthesized)} from kalshi_markets)."
    )

    # Base config — mirrors current production .env (hard stop disabled,
    # 60s same-side cooldown, health score at 25, health breach at 3 ticks).
    base_cfg = {
        "mode": "paper",
        "hard_stop_loss_pct": 0.0,
        "paper_same_side_cooldown_sec": args.paper_cooldown_sec,
        "paper_thesis_flip_unlock_enabled": True,
        "health_score_threshold": args.health_score_threshold,
        "health_score_breach_ticks": args.health_score_breach_ticks,
        "exit_intelligence_enabled": True,
        "exit_intelligence_shadow_only": False,
        "health_exit_confirmation_enabled": True,
        "health_exit_confirmation_roc_delta": 0.05,
        "health_exit_confirmation_obi_delta": 0.05,
        "health_exit_confirmation_neutral_obi": 0.50,
        "exit_fill_mode": "mark",
        "ml_gate_mode": "disabled",
        # short_threshold is mirrored: 1 - long_threshold (set per run below)
    }

    thresholds = [round(t, 2) for t in args.long_threshold_values]
    print(f"Sweeping long_threshold values: {thresholds}")
    print()

    results = []
    for thresh in thresholds:
        # Mirror short threshold: OBI below (1 - long_thresh) triggers short.
        # This preserves the symmetric band around 0.50.
        short_thresh = round(1.0 - thresh, 2)
        cfg = {
            **base_cfg,
            "long_threshold": thresh,
            "short_threshold": short_thresh,
        }
        bt = ContractBacktester(
            candles=candles,
            contract_timelines=timelines,
            config=cfg,
            settlement_data=settlements,
        )
        result = bt.run(bankroll=args.bankroll)
        summary = _summarize(thresh, result, bt.trades)
        results.append(summary)

        wr = summary["win_rate_pct"]
        pnl = summary["total_pnl"]
        n = summary["total_trades"]
        avg = summary["avg_pnl"]
        h17 = summary["h17_counterfactual"]["h17_pnl"]
        print(
            f"  thresh={thresh:.2f} (short={short_thresh:.2f}) | "
            f"n={n:4d} | WR={wr:4.1f}% | PnL=${pnl:8,.2f} | "
            f"avg=${avg:6.2f} | h17_drag=${h17:6.2f}"
        )

    # Sort by avg_pnl descending for easy reading.
    results_sorted = sorted(results, key=lambda r: r["avg_pnl"], reverse=True)

    output = {
        "window": {"start": args.start, "end": args.end},
        "base_config": {
            k: v for k, v in base_cfg.items()
            if k not in ("long_threshold", "short_threshold")
        },
        "current_production_threshold": 0.65,
        "results_by_threshold": results,
        "ranked_by_avg_pnl": [
            {"rank": i + 1, "long_threshold": r["long_threshold"],
             "win_rate_pct": r["win_rate_pct"], "avg_pnl": r["avg_pnl"],
             "total_pnl": r["total_pnl"], "total_trades": r["total_trades"]}
            for i, r in enumerate(results_sorted)
        ],
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\nFull results written to {out_path}")

    print("\n=== Ranked by avg_pnl ===")
    print(f"{'Rank':>4}  {'threshold':>9}  {'WR%':>6}  {'avg_pnl':>8}  {'total_pnl':>10}  {'trades':>6}")
    for row in output["ranked_by_avg_pnl"]:
        marker = "  ← current" if row["long_threshold"] == 0.65 else ""
        print(
            f"{row['rank']:>4}  {row['long_threshold']:>9.2f}  "
            f"{row['win_rate_pct']:>6.1f}  {row['avg_pnl']:>8.2f}  "
            f"{row['total_pnl']:>10,.2f}  {row['total_trades']:>6}{marker}"
        )

    return output


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-04-16")
    parser.add_argument("--end", default="2026-05-11")
    parser.add_argument("--series", default="KXBTC")
    parser.add_argument(
        "--long-threshold-values",
        nargs="+",
        type=float,
        default=[0.50, 0.52, 0.55, 0.57, 0.60, 0.62, 0.65, 0.67, 0.70],
        dest="long_threshold_values",
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
        default="backtest_reports/obi_threshold_sweep.json",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    _main()
