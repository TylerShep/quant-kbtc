"""
Data loader — fetch candles + OB snapshots from TimescaleDB or CSV files.
Per the backtesting-framework skill.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

import numpy as np

MIN_CANDLES = 2000
RECOMMENDED_CANDLES = 17520
IDEAL_CANDLES = 35040


def load_candles_csv(path: str | Path) -> list[dict]:
    """Load 15m candles from a CSV file (Binance format)."""
    candles = []
    with open(path) as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 6:
                continue
            try:
                candles.append(
                    {
                        "timestamp": float(row[0]),
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
    """Load candles from TimescaleDB."""
    async with pool.connection() as conn:
        rows = await conn.execute(
            """SELECT timestamp, open, high, low, close, volume
               FROM candles
               WHERE symbol = %s AND source = %s
               ORDER BY timestamp ASC
               LIMIT %s""",
            (symbol, source, limit),
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
    query = "SELECT timestamp, bids, asks, obi FROM ob_snapshots"
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
            r[0].timestamp(): {"bids": r[1], "asks": r[2], "obi": float(r[3]) if r[3] else 0.5}
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
