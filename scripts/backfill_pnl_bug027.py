#!/usr/bin/env python3
"""
BUG-027: Backfill historical mid-flight live PnL using the cash-flow formula.

Before BUG-027 was fixed, ``PositionManager._exit_inner`` computed
mid-flight PnL as::

    contracts * $1.00 - entry_cost - exit_cost - (entry_fees + exit_fees)

That formula (a) invented a payout the trade never received (the $1.00
max only materializes at settlement) and (b) treated the *proceeds* of
the closing sale as a second outflow, which double-counted ``exit_cost``
on every long mid-flight exit. The corrected formula is::

    pnl = exit_cost - entry_cost - (entry_fees + exit_fees)

This script rewrites the ``pnl`` / ``pnl_pct`` / ``pnl_drift`` columns on
the affected ``trades`` rows in-place. It only touches rows that:

  * ``trading_mode = 'live'`` (paper trades use a different code path)
  * ``entry_cost_dollars IS NOT NULL AND exit_cost_dollars IS NOT NULL``
    (otherwise the cost-based formula wasn't used at the time -- nothing
    we can do without re-deriving from order ids)
  * ``exit_reason NOT IN
    ('CONTRACT_SETTLED', 'EXPIRY_409_SETTLED',
     'ORPHAN_RECOVERY', 'ORPHAN_SETTLED')``
    (these paths use their own correct PnL formula and were never affected)

The script does NOT touch ``wallet_pnl`` -- that value was computed against
a post-entry wallet snapshot so it's stale by the entry cost and not
recoverable from the row alone. Drift is recomputed against the corrected
pnl so analytics will at least show the new (smaller, more honest) gap.

Defaults to ``--dry-run``. Use ``--apply`` to commit changes. Always
backup the DB first; this is a destructive overwrite.

Usage:
    # Local dev DB, dry run first:
    DATABASE_URL=postgresql://kalshi:kalshi_secret@localhost:5433/kbtc \
        python3 scripts/backfill_pnl_bug027.py

    # Local dev DB, apply changes:
    DATABASE_URL=... python3 scripts/backfill_pnl_bug027.py --apply

    # Remote (run inside the bot container so it sees DATABASE_URL):
    ssh "$KBTC_DEPLOY_HOST" \
        "docker exec kbtc-bot python3 /app/scripts/backfill_pnl_bug027.py --apply"

    # Limit blast radius to a single trade (useful for spot checks):
    python3 scripts/backfill_pnl_bug027.py --trade-id 750 --apply
"""
from __future__ import annotations

import argparse
import os
import sys


SETTLEMENT_REASONS = (
    "CONTRACT_SETTLED",
    "EXPIRY_409_SETTLED",
    "ORPHAN_RECOVERY",
    "ORPHAN_SETTLED",
)


def fetch_affected(conn, trade_id: int | None) -> list[tuple]:
    """Pull the rows we need to recompute.

    Returns rows shaped as ``(id, ticker, direction, exit_reason,
    contracts, entry_cost, exit_cost, fees, old_pnl, old_pnl_pct,
    old_pnl_drift, wallet_pnl)``.
    """
    sql = """
        SELECT id, ticker, direction, exit_reason, contracts,
               entry_cost_dollars, exit_cost_dollars, fees,
               pnl, pnl_pct, pnl_drift, wallet_pnl
        FROM trades
        WHERE trading_mode = 'live'
          AND entry_cost_dollars IS NOT NULL
          AND exit_cost_dollars IS NOT NULL
          AND NOT (exit_reason = ANY(%s))
    """
    # psycopg3 requires lists (not tuples) to bind to a Postgres array.
    params: tuple = (list(SETTLEMENT_REASONS),)
    if trade_id is not None:
        sql += " AND id = %s"
        params = (list(SETTLEMENT_REASONS), trade_id)
    sql += " ORDER BY id"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def recompute(row: tuple) -> tuple[float, float, float | None]:
    """Return ``(new_pnl, new_pnl_pct, new_drift)`` for one row.

    ``new_drift`` is ``None`` when the original ``wallet_pnl`` was NULL
    (we have nothing to compare against)."""
    (_id, _ticker, _direction, _reason, _contracts,
     entry_cost, exit_cost, fees,
     _old_pnl, _old_pct, _old_drift, wallet_pnl) = row

    entry_cost = float(entry_cost)
    exit_cost = float(exit_cost)
    fees = float(fees) if fees is not None else 0.0

    new_pnl = round(exit_cost - entry_cost - fees, 4)
    notional = entry_cost if entry_cost > 0 else 1.0
    new_pct = round(new_pnl / notional, 4)
    new_drift: float | None
    if wallet_pnl is not None:
        new_drift = round(abs(new_pnl - float(wallet_pnl)), 4)
    else:
        new_drift = None
    return new_pnl, new_pct, new_drift


def update_row(conn, trade_id: int, new_pnl: float, new_pct: float,
               new_drift: float | None) -> None:
    sql = """
        UPDATE trades
        SET pnl = %s,
            pnl_pct = %s,
            pnl_drift = %s
        WHERE id = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (new_pnl, new_pct, new_drift, trade_id))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rewrite mid-flight live PnL using the BUG-027 cash-flow formula.",
    )
    parser.add_argument("--db-url", type=str, default=None,
                        help="PostgreSQL URL (default: DATABASE_URL env)")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write changes (default: dry run only).")
    parser.add_argument("--trade-id", type=int, default=None,
                        help="Restrict to a single trades.id row (debugging).")
    args = parser.parse_args()

    db_url = args.db_url or os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: No database URL. Set DATABASE_URL env var or use --db-url.",
              file=sys.stderr)
        return 1

    try:
        import psycopg
    except ImportError:
        print("ERROR: psycopg not installed. Run: pip install psycopg[binary]",
              file=sys.stderr)
        return 1

    delta_total = 0.0
    rows_changed = 0
    rows_unchanged = 0
    samples: list[tuple] = []

    with psycopg.connect(db_url) as conn:
        affected = fetch_affected(conn, args.trade_id)
        if not affected:
            print("No affected rows. Nothing to backfill.")
            return 0
        print(f"Found {len(affected)} candidate rows.\n")
        print(f"{'id':>5}  {'ticker':<28} {'dir':<5} {'reason':<14} "
              f"{'old_pnl':>9} → {'new_pnl':>9}  {'delta':>9}")
        print("-" * 95)

        for row in affected:
            (tid, ticker, direction, reason, _ct,
             _ec, _xc, _f, old_pnl, _oldpct, _olddrift, _wp) = row
            new_pnl, new_pct, new_drift = recompute(row)
            old_pnl_f = float(old_pnl) if old_pnl is not None else 0.0
            delta = round(new_pnl - old_pnl_f, 4)

            if abs(delta) < 1e-4:
                rows_unchanged += 1
                continue

            rows_changed += 1
            delta_total += delta
            ticker_disp = ticker[:28]
            print(f"{tid:>5}  {ticker_disp:<28} {direction:<5} {reason:<14} "
                  f"{old_pnl_f:>9.4f} → {new_pnl:>9.4f}  {delta:>+9.4f}")

            if args.apply:
                update_row(conn, tid, new_pnl, new_pct, new_drift)

            if len(samples) < 5:
                samples.append((tid, old_pnl_f, new_pnl, delta))

        if args.apply:
            conn.commit()

    print("-" * 95)
    print(f"\nRows changed:    {rows_changed}")
    print(f"Rows unchanged:  {rows_unchanged}")
    print(f"Total PnL delta: {delta_total:+.4f}  (sum of new_pnl - old_pnl)")
    if not args.apply:
        print("\nDRY RUN -- no changes written. Re-run with --apply to commit.")
    else:
        print("\nApplied. Verify a spot check then re-run analytics / "
              "scripts/backfill_attribution.py to refresh daily_attribution.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
