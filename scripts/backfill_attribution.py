#!/usr/bin/env python3
"""
Backfill historical daily_attribution rows.

Reads every (DATE(timestamp), trading_mode) pair from `trades`, runs the
attribution calculator on each day's trades, and upserts the result into
`daily_attribution`. Safe to re-run — uses ON CONFLICT (date, trading_mode).

Run this once after migration 005_fix_daily_attribution_pk.sql so the
weekly digest has historical data to summarize.

Usage:
    # Local dev DB
    DATABASE_URL=postgresql://kalshi:kalshi_secret@localhost:5433/kbtc \
        python3 scripts/backfill_attribution.py

    # Remote (run inside the bot container so it sees DATABASE_URL):
    ssh botuser@167.71.247.154 "docker exec kbtc-bot python3 /app/scripts/backfill_attribution.py"

    # Restrict to one mode:
    python3 scripts/backfill_attribution.py --mode paper

    # Dry-run (compute but do not insert):
    python3 scripts/backfill_attribution.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# Host: scripts/ lives next to backend/. Container: this script may be run
# from /app where `backtesting/` is a top-level package. Add both candidates.
for _candidate in (os.path.join(_HERE, "..", "backend"), os.path.join(_HERE, "..")):
    _candidate = os.path.abspath(_candidate)
    if os.path.isdir(os.path.join(_candidate, "backtesting")):
        sys.path.insert(0, _candidate)

from backtesting.attribution import run_attribution  # noqa: E402


def fetch_distinct_day_modes(conn, mode_filter: str | None) -> list[tuple]:
    """Return list of (date, trading_mode) pairs that have at least one trade."""
    sql = """
        SELECT DISTINCT DATE(timestamp) AS d, trading_mode
        FROM trades
        WHERE timestamp IS NOT NULL
    """
    params: tuple = ()
    if mode_filter:
        sql += " AND trading_mode = %s"
        params = (mode_filter,)
    sql += " ORDER BY d, trading_mode"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_trades_for_day(conn, date_iso: str, mode: str) -> list[dict]:
    """Pull trades for one (date, mode) and shape them for run_attribution()."""
    sql = """
        SELECT timestamp, direction, pnl, pnl_pct, fees,
               exit_reason, conviction, regime_at_entry,
               candles_held, closed_at
        FROM trades
        WHERE DATE(timestamp) = %s AND trading_mode = %s
        ORDER BY timestamp
    """
    with conn.cursor() as cur:
        cur.execute(sql, (date_iso, mode))
        rows = cur.fetchall()

    trades: list[dict] = []
    for r in rows:
        trades.append({
            "timestamp": r[0].timestamp() if r[0] else 0,
            "direction": r[1],
            "pnl": float(r[2]) if r[2] is not None else 0,
            "pnl_pct": float(r[3]) if r[3] is not None else 0,
            "fees": float(r[4]) if r[4] is not None else 0,
            "exit_reason": r[5],
            "conviction": r[6],
            "regime_at_entry": r[7],
            "candles_held": r[8],
            "exit_timestamp": r[9].timestamp() if r[9] else 0,
        })
    return trades


def upsert_attribution(conn, date_iso: str, mode: str, attr: dict) -> None:
    sql = """
        INSERT INTO daily_attribution
                (date, total_trades, total_pnl, attribution, trading_mode)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (date, trading_mode) DO UPDATE
        SET total_trades = EXCLUDED.total_trades,
            total_pnl    = EXCLUDED.total_pnl,
            attribution  = EXCLUDED.attribution
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                date_iso,
                attr.get("total_trades", 0),
                attr.get("total_pnl_dollars", 0),
                json.dumps(attr),
                mode,
            ),
        )


def main():
    parser = argparse.ArgumentParser(
        description="Backfill daily_attribution rows from historical trades."
    )
    parser.add_argument("--db-url", type=str, default=None,
                        help="PostgreSQL URL (default: DATABASE_URL env)")
    parser.add_argument("--mode", type=str, choices=["live", "paper"],
                        default=None,
                        help="Backfill only this mode (default: both)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute attribution but do not write")
    args = parser.parse_args()

    db_url = args.db_url or os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: No database URL. Set DATABASE_URL env var or use --db-url.",
              file=sys.stderr)
        sys.exit(1)

    try:
        import psycopg
    except ImportError:
        print("ERROR: psycopg not installed. Run: pip install psycopg[binary]",
              file=sys.stderr)
        sys.exit(1)

    written = 0
    skipped = 0
    with psycopg.connect(db_url) as conn:
        pairs = fetch_distinct_day_modes(conn, args.mode)
        if not pairs:
            print("No trades found. Nothing to backfill.")
            return
        print(f"Found {len(pairs)} (date, mode) pairs to process.")

        for date_obj, mode in pairs:
            date_iso = date_obj.isoformat()
            trades = fetch_trades_for_day(conn, date_iso, mode)
            if not trades:
                skipped += 1
                continue
            attr = run_attribution(trades)
            total_pnl = attr.get("total_pnl_dollars", 0)
            n_trades = attr.get("total_trades", 0)
            print(f"  {date_iso} [{mode:5s}] trades={n_trades:3d} pnl=${total_pnl:>9.2f}",
                  end="")
            if args.dry_run:
                print("  (dry-run)")
            else:
                upsert_attribution(conn, date_iso, mode, attr)
                written += 1
                print("  upserted")
        if not args.dry_run:
            conn.commit()

    print(f"\nDone. Upserted {written} rows; skipped {skipped} empty days.")


if __name__ == "__main__":
    main()
