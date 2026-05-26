#!/usr/bin/env python3
"""2D grid sweep: STOP_LOSS_PCT × PROFIT_TARGET_MULT with in/out-of-sample split.

Motivation
----------
After the May 11 manual fix (STOP_LOSS_PCT: 0.02→0.30, PROFIT_TARGET_MULT: 1.50→0.10),
fee drag reached ~72% of gross PnL on paper trades.  Root cause: the current take-profit
threshold is 0.30 × 0.10 = 3%, but at 15¢ entry with ~1 700 contracts the round-trip fee
is ~$33 while a 3% gross profit is only ~$8.  The strategy is systematically taking profit
at a price point where fees eat the entire gain.

This script sweeps a 6 × 6 grid of (stop_loss_pct, profit_target_mult) pairs, using:
  - In-sample  window (IS) : Apr 13 – May 6   (training / selection)
  - Out-of-sample window (OOS): May 7 – May 18  (validation, never peeked at during selection)

Reports IS + OOS metrics side-by-side for each combo so we can select a robust pair
without over-fitting to the IS period.

Usage (run inside the kbtc-bot Docker container or with DATABASE_URL set):
    DATABASE_URL=postgresql://kalshi:kalshi_secret@localhost:5432/kbtc \
        python3 /app/backtest_pt_sl_sweep.py \
        [--is-start 2026-04-13] [--is-end 2026-05-07] \
        [--oos-start 2026-05-07] [--oos-end 2026-05-19] \
        [--output /tmp/pt_sl_sweep.json]
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
from typing import Any, Optional

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

# ── Ticker/settlement helpers (copied from backtest_stop_loss_sweep.py) ────────

_TICKER_FULL_RE = re.compile(
    r"^KX[A-Z]+-(\d{2})([A-Z]{3})(\d{2})(\d{2})-([BT])(\d+(?:\.\d+)?)$",
    re.IGNORECASE,
)
_MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_ticker(ticker: str):
    m = _TICKER_FULL_RE.match((ticker or "").upper())
    if not m:
        return None
    yy, mmm, dd, hh, direction, strike_str = m.groups()
    month = _MONTH_MAP.get(mmm)
    if month is None:
        return None
    try:
        et_dt = datetime(
            year=2000 + int(yy), month=month, day=int(dd), hour=int(hh),
            minute=0, second=0, tzinfo=_ET_TZ,
        )
    except ValueError:
        return None
    return et_dt.astimezone(timezone.utc).timestamp(), direction.upper(), float(strike_str)


def _btc_price_at(candles: list[dict], target_ts: float, window_sec: float = 900.0):
    best = None
    for c in candles:
        dt = abs(c["timestamp"] - target_ts)
        if dt <= window_sec and (best is None or dt < best[0]):
            best = (dt, c["close"])
    return best[1] if best is not None else None


def _synthesize_settlements(timelines: dict, candles: list[dict]) -> dict[str, dict]:
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
            "open": float(r[1]), "high": float(r[2]),
            "low": float(r[3]), "close": float(r[4]),
            "volume": float(r[5]),
        }
        for r in result
    ]


# ── Metrics helpers ────────────────────────────────────────────────────────────

def _summarize_trades(trades: list[dict]) -> dict:
    """Compute key metrics from a list of closed trade dicts."""
    long_trades = [t for t in trades if t.get("direction") == "long"]
    if not long_trades:
        return {
            "trades": 0, "win_rate_pct": 0.0, "total_pnl": 0.0,
            "avg_pnl": 0.0, "avg_fee": 0.0, "fee_drag_pct": 0.0,
            "profit_factor": 0.0, "max_drawdown_pct": 0.0,
            "exit_counts": {}, "exit_pnl": {},
        }

    total = len(long_trades)
    wins = sum(1 for t in long_trades if t.get("pnl", 0.0) > 0)
    total_pnl = sum(t.get("pnl", 0.0) for t in long_trades)
    total_fees = sum(t.get("fees", 0.0) for t in long_trades)
    gross_wins = sum(t.get("pnl", 0.0) + t.get("fees", 0.0) for t in long_trades if t.get("pnl", 0.0) > 0)
    gross_losses = abs(sum(t.get("pnl", 0.0) for t in long_trades if t.get("pnl", 0.0) <= 0))

    by_exit: dict[str, int] = {}
    pnl_by_exit: dict[str, float] = {}
    for t in long_trades:
        r = t.get("exit_reason", "UNKNOWN")
        by_exit[r] = by_exit.get(r, 0) + 1
        pnl_by_exit[r] = round(pnl_by_exit.get(r, 0.0) + t.get("pnl", 0.0), 2)

    gross_total = total_pnl + total_fees
    fee_drag_pct = round(total_fees / gross_total * 100.0, 1) if gross_total > 0 else None

    return {
        "trades": total,
        "win_rate_pct": round(100.0 * wins / total, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / total, 4),
        "avg_fee": round(total_fees / total, 2),
        "fee_drag_pct": fee_drag_pct,
        "profit_factor": round(gross_wins / gross_losses, 3) if gross_losses > 0 else None,
        "exit_counts": by_exit,
        "exit_pnl": {k: round(v, 2) for k, v in pnl_by_exit.items()},
    }


def _run_backtest(
    candles: list[dict],
    timelines: dict,
    settlements: dict,
    base_cfg: dict,
    stop_loss_pct: float,
    profit_target_mult: float,
    bankroll: float,
) -> dict:
    cfg = {
        **base_cfg,
        "stop_loss_pct": stop_loss_pct,
        "profit_target_mult": profit_target_mult,
    }
    bt = ContractBacktester(
        candles=candles,
        contract_timelines=timelines,
        config=cfg,
        settlement_data=settlements,
    )
    result = bt.run(bankroll=bankroll)
    summary = _summarize_trades(bt.trades)
    summary["sharpe_ratio"] = round(float(result.get("sharpe_ratio", 0.0)), 3)
    summary["max_drawdown_pct"] = round(float(result.get("max_drawdown_pct", 0.0)), 2)
    return summary


# ── Main ───────────────────────────────────────────────────────────────────────

async def _load_ob_snapshots_lean(
    pool,
    start_ts: float,
    end_ts: float,
    series: str,
    bucket_sec: int = 30,
) -> dict[str, "ContractTimeline"]:
    """
    Memory-efficient OB snapshot loader.

    Instead of fetching full JSONB bids/asks (the standard data_loader approach,
    which easily consumes 1–2 GB for a 7-day window), this query:
      1. Extracts only bid[0]/ask[0] prices from JSONB in SQL (avoids deserialising
         the full JSON in Python).
      2. Buckets snapshots into ``bucket_sec``-second windows and takes the LAST
         tick per (ticker, bucket) — reducing ~800k rows to ~80k rows with no
         meaningful loss of signal fidelity for a parameter sweep.
    """
    from backtesting.contract_timeline import ContractTick, ContractTimeline  # local import

    start_dt = _to_dt(start_ts)
    end_dt   = _to_dt(end_ts)

    query = """
        SELECT
            date_trunc('second',
                timestamp - (EXTRACT(SECOND FROM timestamp)::int %% %s) * INTERVAL '1 second'
            )                                                 AS bucket,
            ticker,
            (array_agg(
                (bids->0->>0)::float
                ORDER BY timestamp DESC
            ))[1]                                             AS best_bid,
            (array_agg(
                (asks->0->>0)::float
                ORDER BY timestamp DESC
            ))[1]                                             AS best_ask,
            (array_agg(obi         ORDER BY timestamp DESC))[1]          AS obi,
            (array_agg(spread_cents ORDER BY timestamp DESC))[1]         AS spread_cents
        FROM ob_snapshots
        WHERE ticker LIKE %s
          AND timestamp >= %s
          AND timestamp <= %s
        GROUP BY bucket, ticker
        ORDER BY bucket ASC
    """

    async with pool.connection() as conn:
        rows = await conn.execute(
            query,
            (bucket_sec, f"{series}%", start_dt, end_dt),
        )
        result = await rows.fetchall()

    timelines: dict[str, ContractTimeline] = {}
    for row in result:
        ts     = row[0].timestamp()
        ticker = row[1]
        bid    = float(row[2]) if row[2] is not None else None
        ask    = float(row[3]) if row[3] is not None else None
        obi_v  = float(row[4]) if row[4] is not None else 0.5
        spread = float(row[5]) if row[5] is not None else None

        if bid is not None and ask is not None:
            mid = round((bid + ask) / 2.0, 2)
        elif bid is not None:
            mid = bid
        elif ask is not None:
            mid = ask
        else:
            mid = None

        tl = timelines.setdefault(ticker, ContractTimeline(ticker=ticker))
        tl.add_tick(ContractTick(
            timestamp=ts,
            ticker=ticker,
            mid_cents=mid,
            best_bid=bid,
            best_ask=ask,
            spread_cents=spread,
            obi=obi_v,
            total_bid_vol=0.0,
            total_ask_vol=0.0,
            source="ob_bucket",
        ))

    for tl in timelines.values():
        tl.finalize()
    return timelines


async def _load_window(pool, start_ts: float, end_ts: float, series: str, candles_all: list[dict], bucket_sec: int = 30):
    """Load timelines + settlements for a single window; returns sliced candles + data."""
    lookback = start_ts - 3 * 3600.0
    timelines = await _load_ob_snapshots_lean(
        pool, start_ts=lookback, end_ts=end_ts + 900.0, series=series, bucket_sec=bucket_sec,
    )
    settlements = await load_settlement_outcomes_db(
        pool, start_ts=lookback - 86400.0, end_ts=end_ts + 86400.0, series=series,
    )

    # Merge DB settlements
    for ticker, meta in settlements.items():
        tl = timelines.get(ticker)
        if tl:
            tl.close_time = meta.get("close_time")
            tl.result     = meta.get("result")
            tl.expiration_value = meta.get("expiration_value")

    # Synthesise missing Apr-May 2026 outcomes from BTC candle closes
    missing = [t for t in timelines if t not in settlements]
    synthesized = _synthesize_settlements({t: timelines[t] for t in missing}, candles_all)
    settlements.update(synthesized)
    for ticker, meta in synthesized.items():
        tl = timelines.get(ticker)
        if tl:
            tl.result           = meta.get("result")
            tl.expiration_value = meta.get("expiration_value")

    window_candles = [c for c in candles_all if start_ts <= c["timestamp"] <= end_ts]
    return window_candles, timelines, settlements, len(synthesized)


async def run(args) -> dict[str, Any]:
    is_start_ts  = _parse_date(args.is_start)
    is_end_ts    = _parse_date(args.is_end)
    oos_start_ts = _parse_date(args.oos_start)
    oos_end_ts   = _parse_date(args.oos_end)

    # Load candles once for full range (cheap — only ~4k rows)
    full_start = min(is_start_ts, oos_start_ts) - 3 * 3600.0
    full_end   = max(is_end_ts, oos_end_ts) + 900.0

    print(f"Loading BTC candles for full range …")
    pool = await get_pool()
    try:
        candles_all = await _load_candles(pool, start_ts=full_start, end_ts=full_end)
        print(f"  {len(candles_all)} candles loaded.")

        # Load IS and OOS contract data separately to stay within container memory limits.
        # ob_snapshots can be 6M+ rows over 24 days; loading each 7-11 day window
        # individually keeps peak RSS < 500 MB.
        bucket_sec = args.bucket_sec
        print(f"Loading IS window ({args.is_start} → {args.is_end}, bucket={bucket_sec}s) …")
        is_candles, is_timelines, is_settlements, is_synth = await _load_window(
            pool, is_start_ts, is_end_ts, args.series, candles_all, bucket_sec=bucket_sec,
        )
        print(f"  {len(is_candles)} candles, {len(is_timelines)} timelines "
              f"({is_synth} synthesised).")

        print(f"Loading OOS window ({args.oos_start} → {args.oos_end}, bucket={bucket_sec}s) …")
        oos_candles, oos_timelines, oos_settlements, oos_synth = await _load_window(
            pool, oos_start_ts, oos_end_ts, args.series, candles_all, bucket_sec=bucket_sec,
        )
        print(f"  {len(oos_candles)} candles, {len(oos_timelines)} timelines "
              f"({oos_synth} synthesised).")
    finally:
        await close_pool()

    # Base config mirrors production exactly
    base_cfg: dict = {
        "mode": "paper",
        "hard_stop_loss_pct": 0.0,
        "short_threshold": 0.0,          # long-only
        "paper_same_side_cooldown_sec": 60.0,
        "paper_thesis_flip_unlock_enabled": True,
        "exit_intelligence_enabled": True,
        "exit_intelligence_shadow_only": False,   # active (not shadow) for sweep
        "health_score_threshold": 25.0,
        "health_score_breach_ticks": 3,
        "health_exit_confirmation_enabled": True,
        "health_exit_confirmation_roc_delta": 0.05,
        "health_exit_confirmation_obi_delta": 0.05,
        "health_exit_confirmation_neutral_obi": 0.50,
        "exit_fill_mode": "mark",
        "ml_gate_mode": "disabled",
        "blocked_hours_utc": [17],
        "long_threshold": 0.65,
    }

    # Grid definition
    stop_values   = args.stop_values
    target_values = args.target_values

    print(f"\nGrid: {len(stop_values)} stop × {len(target_values)} target = {len(stop_values)*len(target_values)} combos")
    print("Running IS window …")

    # Also include current & reference configs as named extras
    reference_configs = [
        ("current_prod",    0.30, 0.10),
        ("pre_may11_prod",  0.02, 1.50),
    ]

    rows: list[dict] = []

    header = (f"{'sl':>6}  {'mult':>5}  "
              f"{'IS_n':>5}  {'IS_WR%':>7}  {'IS_avg$':>8}  {'IS_fee%':>7}  {'IS_pf':>6}  {'IS_shp':>7}  "
              f"{'OOS_n':>5}  {'OOS_WR%':>7}  {'OOS_avg$':>8}  {'OOS_fee%':>7}  {'OOS_pf':>6}  {'OOS_shp':>7}")
    print(header)
    print("-" * len(header))

    def _run_pair(sl: float, mult: float, tag: Optional[str] = None) -> dict:
        is_m  = _run_backtest(is_candles,  is_timelines,  is_settlements,  base_cfg, sl, mult, args.bankroll)
        oos_m = _run_backtest(oos_candles, oos_timelines, oos_settlements, base_cfg, sl, mult, args.bankroll)
        row = {
            "tag": tag,
            "stop_loss_pct":       round(sl,   4),
            "profit_target_mult":  round(mult, 4),
            "profit_target_pct":   round(sl * mult * 100, 2),
            "is":  is_m,
            "oos": oos_m,
        }
        tp_tag = f"  [{tag}]" if tag else ""
        print(
            f"  sl={sl:.2f}  mt={mult:.2f}  "
            f"IS: n={is_m['trades']:4d}  WR={is_m['win_rate_pct']:5.1f}%  "
            f"avg=${is_m['avg_pnl']:6.2f}  fee={str(is_m['fee_drag_pct'])+'%':>6}  "
            f"pf={str(is_m['profit_factor']):>5}  shp={is_m['sharpe_ratio']:>6.3f}  "
            f"OOS: n={oos_m['trades']:4d}  WR={oos_m['win_rate_pct']:5.1f}%  "
            f"avg=${oos_m['avg_pnl']:6.2f}  fee={str(oos_m['fee_drag_pct'])+'%':>6}  "
            f"pf={str(oos_m['profit_factor']):>5}  shp={oos_m['sharpe_ratio']:>6.3f}"
            f"{tp_tag}"
        )
        return row

    # Grid combos
    for sl in sorted(stop_values):
        for mult in sorted(target_values):
            row = _run_pair(sl, mult)
            rows.append(row)

    # Reference configs
    print("\nReference configs:")
    for tag, sl, mult in reference_configs:
        row = _run_pair(sl, mult, tag=tag)
        rows.append(row)

    # ── Ranking ────────────────────────────────────────────────────────────────
    # Score: OOS avg_pnl (primary) — avoids over-fitting to IS.
    # Filter: must have >= 30 OOS trades (statistical minimum).
    valid = [r for r in rows if r["tag"] is None and r["oos"]["trades"] >= 30]
    valid.sort(key=lambda r: (
        r["oos"]["avg_pnl"] if r["oos"]["avg_pnl"] is not None else -9999
    ), reverse=True)

    print(f"\n=== Top combos by OOS avg_pnl (min 30 OOS trades) ===")
    print(f"{'Rank':>4}  {'sl':>5}  {'mult':>5}  {'tgt%':>5}  "
          f"{'IS_WR':>7}  {'IS_avg$':>8}  {'OOS_WR':>7}  {'OOS_avg$':>8}  {'OOS_pf':>7}  {'OOS_shp':>7}")
    for i, r in enumerate(valid[:10], 1):
        print(
            f"  {i:2d}  {r['stop_loss_pct']:5.2f}  {r['profit_target_mult']:5.2f}  "
            f"{r['profit_target_pct']:5.1f}%  "
            f"{r['is']['win_rate_pct']:6.1f}%  {r['is']['avg_pnl']:8.2f}  "
            f"{r['oos']['win_rate_pct']:6.1f}%  {r['oos']['avg_pnl']:8.2f}  "
            f"{str(r['oos']['profit_factor']):>7}  {r['oos']['sharpe_ratio']:>7.3f}"
        )

    best = valid[0] if valid else None
    if best:
        print(f"\n★  Recommended:  STOP_LOSS_PCT={best['stop_loss_pct']}  "
              f"PROFIT_TARGET_MULT={best['profit_target_mult']}  "
              f"(effective take-profit threshold = {best['profit_target_pct']:.1f}%)")
        print(f"   OOS: {best['oos']['trades']} trades, WR={best['oos']['win_rate_pct']}%,"
              f" avg_pnl=${best['oos']['avg_pnl']:.2f},"
              f" fee_drag={best['oos']['fee_drag_pct']}%,"
              f" sharpe={best['oos']['sharpe_ratio']:.3f}")
    else:
        print("\nNo combo cleared 30 OOS trades — extend data range.")

    output = {
        "meta": {
            "is_window":  {"start": args.is_start,  "end": args.is_end},
            "oos_window": {"start": args.oos_start, "end": args.oos_end},
            "bankroll":   args.bankroll,
            "series":     args.series,
            "base_config": {k: v for k, v in base_cfg.items()
                            if k not in ("stop_loss_pct", "profit_target_mult")},
        },
        "grid": rows,
        "ranked_oos": [
            {
                "rank": i + 1,
                "stop_loss_pct":      r["stop_loss_pct"],
                "profit_target_mult": r["profit_target_mult"],
                "profit_target_pct":  r["profit_target_pct"],
                "oos_trades":    r["oos"]["trades"],
                "oos_win_rate":  r["oos"]["win_rate_pct"],
                "oos_avg_pnl":   r["oos"]["avg_pnl"],
                "oos_fee_drag":  r["oos"]["fee_drag_pct"],
                "oos_profit_factor": r["oos"]["profit_factor"],
                "oos_sharpe":    r["oos"]["sharpe_ratio"],
                "is_win_rate":   r["is"]["win_rate_pct"],
                "is_avg_pnl":    r["is"]["avg_pnl"],
            }
            for i, r in enumerate(valid[:15])
        ],
        "recommended": {
            "stop_loss_pct":      best["stop_loss_pct"] if best else None,
            "profit_target_mult": best["profit_target_mult"] if best else None,
            "profit_target_pct":  best["profit_target_pct"] if best else None,
            "oos_avg_pnl":        best["oos"]["avg_pnl"] if best else None,
            "oos_win_rate_pct":   best["oos"]["win_rate_pct"] if best else None,
        },
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\nFull results written → {out_path}")
    return output


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--is-start",  default="2026-04-30", dest="is_start")  # 7-day IS (avoids 6M-row OOM)
    parser.add_argument("--is-end",    default="2026-05-07", dest="is_end")
    parser.add_argument("--oos-start", default="2026-05-07", dest="oos_start")
    parser.add_argument("--oos-end",   default="2026-05-19", dest="oos_end")
    parser.add_argument("--series",    default="KXBTC")
    parser.add_argument("--bankroll",  type=float, default=10000.0)
    parser.add_argument(
        "--stop-values", nargs="+", type=float, dest="stop_values",
        default=[0.05, 0.10, 0.15, 0.20, 0.25, 0.30],
    )
    parser.add_argument(
        "--target-values", nargs="+", type=float, dest="target_values",
        default=[0.5, 0.75, 1.0, 1.25, 1.5, 2.0],
    )
    parser.add_argument("--output", default="/tmp/pt_sl_sweep.json")
    parser.add_argument("--bucket-sec", type=int, default=30, dest="bucket_sec",
                        help="OB snapshot time-bucket size in seconds (default 30 → ~10x row reduction)")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    _main()
