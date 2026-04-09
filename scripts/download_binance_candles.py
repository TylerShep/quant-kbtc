"""
Download historical 15m BTC candles from Binance.
Saves to CSV and optionally inserts into TimescaleDB.

Usage:
    python scripts/download_binance_candles.py --months 6 --output data/candles_btc_15m.csv
    python scripts/download_binance_candles.py --months 12 --db
"""
from __future__ import annotations

import argparse
import csv
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "15m"
LIMIT = 1000


def fetch_candles(start_ms: int, end_ms: int) -> list[list]:
    """Fetch up to 1000 candles from Binance REST API."""
    all_candles = []
    current = start_ms

    while current < end_ms:
        params = {
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "startTime": current,
            "endTime": end_ms,
            "limit": LIMIT,
        }

        with httpx.Client(timeout=30.0) as client:
            r = client.get(BINANCE_KLINES_URL, params=params)
            r.raise_for_status()
            data = r.json()

        if not data:
            break

        all_candles.extend(data)
        current = int(data[-1][0]) + 1
        print(f"  Fetched {len(all_candles)} candles so far...")
        time.sleep(0.5)

    return all_candles


def save_csv(candles: list[list], output_path: str):
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp", "open", "high", "low", "close",
            "volume", "close_time", "quote_volume", "trades",
        ])
        for c in candles:
            writer.writerow([
                c[0],
                c[1], c[2], c[3], c[4], c[5],
                c[6], c[7], c[8],
            ])

    print(f"Saved {len(candles)} candles to {path}")


async def insert_db(candles: list[list]):
    import asyncio
    import psycopg

    from config import settings

    async with await psycopg.AsyncConnection.connect(settings.database.url) as conn:
        async with conn.cursor() as cur:
            for c in candles:
                ts = datetime.fromtimestamp(int(c[0]) / 1000, tz=timezone.utc)
                await cur.execute(
                    """INSERT INTO candles (timestamp, source, symbol, open, high, low, close, volume)
                       VALUES (%s, 'binance', 'BTCUSDT', %s, %s, %s, %s, %s)
                       ON CONFLICT (timestamp, source, symbol) DO NOTHING""",
                    (ts, c[1], c[2], c[3], c[4], c[5]),
                )
            await conn.commit()
    print(f"Inserted {len(candles)} candles into DB")


def main():
    parser = argparse.ArgumentParser(description="Download Binance BTC 15m candles")
    parser.add_argument("--months", type=int, default=6, help="Months of history")
    parser.add_argument("--output", type=str, default="data/candles_btc_15m.csv")
    parser.add_argument("--db", action="store_true", help="Insert into TimescaleDB")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=args.months * 30)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    print(f"Downloading {args.months} months of {SYMBOL} {INTERVAL} candles...")
    print(f"  From: {start.isoformat()}")
    print(f"  To:   {now.isoformat()}")

    candles = fetch_candles(start_ms, end_ms)
    print(f"Total candles downloaded: {len(candles)}")

    save_csv(candles, args.output)

    if args.db:
        import asyncio
        asyncio.run(insert_db(candles))


if __name__ == "__main__":
    main()
