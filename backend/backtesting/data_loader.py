"""
Data loader — fetch candles + OB snapshots from TimescaleDB or CSV files.
Per the backtesting-framework skill.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

MIN_CANDLES = 2000
RECOMMENDED_CANDLES = 17520
IDEAL_CANDLES = 35040


def load_candles_csv(path: str | Path) -> list[dict]:
    """Load 15m candles from a CSV file (Binance format).

    Binance CSVs use millisecond timestamps; this normalizes to seconds so
    downstream gap detection (900 s) and OB snapshot lookup work correctly.
    """
    candles = []
    with open(path) as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 6:
                continue
            try:
                ts = float(row[0])
                if ts > 1e12:
                    ts /= 1000.0
                candles.append(
                    {
                        "timestamp": ts,
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]),
                    }
                )
            except (ValueError, IndexError):
                continue
    return candles


async def load_candles_db(pool, symbol: str = "BTCUSDT", source: str = "binance",
                          limit: int = IDEAL_CANDLES) -> list[dict]:
    """Load candles from TimescaleDB.

    ``source`` may be a single value or comma-separated list
    (e.g. ``"live_spot,binance"``).
    """
    sources = [s.strip() for s in source.split(",")]
    placeholders = ",".join(["%s"] * len(sources))
    params: list = [symbol, *sources, limit]
    async with pool.connection() as conn:
        rows = await conn.execute(
            f"""SELECT timestamp, open, high, low, close, volume
               FROM candles
               WHERE symbol = %s AND source IN ({placeholders})
               ORDER BY timestamp ASC
               LIMIT %s""",
            params,
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


async def load_ob_snapshots_db(pool, ticker: Optional[str] = None,
                                limit: int = 100000) -> dict[float, dict]:
    """Load OB snapshots keyed by timestamp for backtest lookup."""
    query = "SELECT timestamp, bids, asks, obi, total_bid_vol, total_ask_vol FROM ob_snapshots"
    params: list = []
    if ticker:
        query += " WHERE ticker = %s"
        params.append(ticker)
    query += " ORDER BY timestamp ASC LIMIT %s"
    params.append(limit)

    async with pool.connection() as conn:
        rows = await conn.execute(query, params)
        result = await rows.fetchall()
        return {
            r[0].timestamp(): {
                "bids": r[1],
                "asks": r[2],
                "obi": float(r[3]) if r[3] else 0.5,
                "total_bid_vol": float(r[4]) if r[4] else 0,
                "total_ask_vol": float(r[5]) if r[5] else 0,
            }
            for r in result
        }


def validate_candles(candles: list[dict]) -> dict:
    """Validate candle data quality."""
    n = len(candles)
    if n == 0:
        return {"valid": False, "reason": "no candles"}

    gaps = 0
    for i in range(1, n):
        dt = candles[i]["timestamp"] - candles[i - 1]["timestamp"]
        if dt > 900 * 1.5:
            gaps += 1

    return {
        "valid": n >= MIN_CANDLES,
        "total_candles": n,
        "gaps": gaps,
        "gap_pct": round(gaps / n * 100, 2) if n > 0 else 0,
        "date_range_days": round((candles[-1]["timestamp"] - candles[0]["timestamp"]) / 86400, 1)
        if n > 1
        else 0,
        "sufficient": n >= RECOMMENDED_CANDLES,
    }


async def export_ob_to_csv(pool, output_path: str | Path, limit: int = 100000) -> int:
    """Export OB snapshots from the DB to CSV for offline backtesting.

    Returns the number of rows exported.
    """
    snapshots = await load_ob_snapshots_db(pool, limit=limit)
    if not snapshots:
        return 0

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "obi", "total_bid_vol", "total_ask_vol"])
        for ts, snap in sorted(snapshots.items()):
            writer.writerow([
                ts,
                snap.get("obi", 0.5),
                snap.get("total_bid_vol", 0),
                snap.get("total_ask_vol", 0),
            ])

    return len(snapshots)
