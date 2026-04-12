#!/usr/bin/env python3
"""
Clean-start script for the Kalshi trading bot.

Truncates trade/signal/KPI tables and resets bot_state so the bot starts
fresh. Supports wiping only a specific trading mode (live or paper) to
preserve the other mode's history.

Usage:
    python scripts/clean_start.py                    # wipe ALL data (interactive)
    python scripts/clean_start.py --mode live --yes  # wipe only live data
    python scripts/clean_start.py --mode paper       # wipe only paper data
    python scripts/clean_start.py --keep-candles     # preserve candle history
    python scripts/clean_start.py --keep-ob          # preserve OB snapshots
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

TABLES_WITH_MODE = [
    "trades",
    "errored_trades",
    "bankroll_history",
    "daily_attribution",
]

TABLES_NO_MODE = [
    "signal_log",
    "param_recommendations",
    "bot_state",
    "latency_metrics",
    "pipeline_health",
]

TABLES_OPTIONAL = {
    "candles": "OHLCV candle history (Binance backfill + live)",
    "ob_snapshots": "Order book snapshots",
}

BACKTEST_REPORT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "backend", "backtest_reports",
)

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
)


def get_existing_tables(conn, candidates: list[str]) -> list[str]:
    """Return only those table names from candidates that exist in the DB."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
        existing = {row[0] for row in cur.fetchall()}
    return [t for t in candidates if t in existing]


def build_truncate_sql(tables: list[str]) -> str:
    return f"TRUNCATE {', '.join(tables)} CASCADE;"


def build_mode_delete_sql(tables: list[str], mode: str) -> list[str]:
    """Build DELETE statements for mode-aware tables (filter by trading_mode)."""
    return [f"DELETE FROM {t} WHERE trading_mode = '{mode}';" for t in tables]


def delete_backtest_reports() -> list[str]:
    deleted = []
    if not os.path.isdir(BACKTEST_REPORT_DIR):
        return deleted
    for pattern in ("*.json", "*.html", "*.csv"):
        for f in glob.glob(os.path.join(BACKTEST_REPORT_DIR, pattern)):
            try:
                os.remove(f)
                deleted.append(os.path.basename(f))
            except OSError:
                pass
    return deleted


def delete_data_csvs() -> list[str]:
    deleted = []
    if not os.path.isdir(DATA_DIR):
        return deleted
    for f in glob.glob(os.path.join(DATA_DIR, "*.csv")):
        try:
            os.remove(f)
            deleted.append(os.path.basename(f))
        except OSError:
            pass
    return deleted


def main():
    parser = argparse.ArgumentParser(description="Clean-start the Kalshi trading bot")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip confirmation prompt")
    parser.add_argument("--mode", type=str, choices=["live", "paper"],
                        default=None,
                        help="Only wipe data for this trading mode (live or paper). "
                             "Omit to wipe ALL data for both modes.")
    parser.add_argument("--keep-candles", action="store_true",
                        help="Preserve candle history (Binance backfill)")
    parser.add_argument("--keep-ob", action="store_true",
                        help="Preserve order book snapshots")
    parser.add_argument("--db-url", type=str, default=None,
                        help="PostgreSQL connection URL (default: from env DATABASE_URL)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print SQL without executing")
    args = parser.parse_args()

    db_url = args.db_url or os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: No database URL. Set DATABASE_URL env var or use --db-url.")
        sys.exit(1)

    try:
        import psycopg
    except ImportError:
        print("ERROR: psycopg not installed. Run: pip install psycopg[binary]")
        sys.exit(1)

    mode = args.mode
    mode_label = f"'{mode}' mode only" if mode else "ALL modes"

    with psycopg.connect(db_url) as conn:
        mode_tables = get_existing_tables(conn, TABLES_WITH_MODE)
        shared_tables = get_existing_tables(conn, TABLES_NO_MODE)

    optional_tables: list[str] = []
    if not args.keep_candles:
        optional_tables.append("candles")
    if not args.keep_ob:
        optional_tables.append("ob_snapshots")
    with psycopg.connect(db_url) as conn:
        optional_tables = get_existing_tables(conn, optional_tables)

    if not mode_tables and not shared_tables and not optional_tables:
        print("No matching tables found in DB. Nothing to do.")
        return

    sql_statements: list[str] = []

    if mode:
        # Mode-specific: DELETE rows with matching trading_mode from mode-aware tables
        if mode_tables:
            sql_statements.extend(build_mode_delete_sql(mode_tables, mode))
        # Shared tables (no trading_mode column) are NOT touched in mode-specific wipe
    else:
        # Full wipe: TRUNCATE everything
        all_tables = mode_tables + shared_tables + optional_tables
        if all_tables:
            sql_statements.append(build_truncate_sql(all_tables))

    if not sql_statements:
        print("No SQL to execute. Nothing to do.")
        return

    print(f"\n=== Kalshi Trading Bot — Clean Start ({mode_label}) ===\n")
    print("This will:")
    if mode:
        print(f"  1. DELETE {mode} rows from: {', '.join(mode_tables) if mode_tables else '(none)'}")
        print(f"     Shared tables PRESERVED: {', '.join(shared_tables) if shared_tables else '(none)'}")
        other = "paper" if mode == "live" else "live"
        print(f"     {other.upper()} data PRESERVED in: {', '.join(mode_tables) if mode_tables else '(none)'}")
    else:
        print(f"  1. TRUNCATE tables: {', '.join(mode_tables + shared_tables + optional_tables)}")
        print("  2. Delete backtest reports (JSON, HTML, CSV)")
        print("  3. Delete data CSVs")
    print()
    for stmt in sql_statements:
        print(f"  SQL: {stmt}")
    print()

    if args.dry_run:
        print("[DRY RUN] No changes made.")
        return

    if not args.yes:
        confirm = input("Are you sure? Type 'CLEAN' to proceed: ")
        if confirm.strip() != "CLEAN":
            print("Aborted.")
            sys.exit(1)

    print("\nConnecting to database...")
    try:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                for stmt in sql_statements:
                    print(f"Executing: {stmt}")
                    cur.execute(stmt)
            conn.commit()
        print("Database operations complete.")
    except Exception as e:
        print(f"ERROR: Database operation failed: {e}")
        sys.exit(1)

    if not mode:
        deleted_reports = delete_backtest_reports()
        if deleted_reports:
            print(f"Deleted {len(deleted_reports)} backtest report files.")
        else:
            print("No backtest reports found to delete.")

        deleted_csvs = delete_data_csvs()
        if deleted_csvs:
            print(f"Deleted {len(deleted_csvs)} data CSV files.")

    print(f"\n=== Clean start complete ({mode_label}) ===")
    print("Next steps:")
    if mode == "live":
        print("  1. Restart the bot — it will sync live bankroll from Kalshi wallet")
        print("  2. Paper trading data was NOT touched")
    elif mode == "paper":
        print("  1. Restart the bot — paper trader resets to initial bankroll")
        print("  2. Live trading data was NOT touched")
    else:
        print("  1. Start the bot — it will sync bankroll from Kalshi wallet")
        print("  2. Any exchange positions will be detected as orphans and handled")
        print("  3. If you cleared candles, re-run: python scripts/download_binance_candles.py")
    print()


if __name__ == "__main__":
    main()
