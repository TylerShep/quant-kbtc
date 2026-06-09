"""
Data loader — fetch candles + OB snapshots from TimescaleDB or CSV files.
Per the backtesting-framework skill.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backtesting.contract_timeline import ContractTick, ContractTimeline

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
                                limit: int = 100000,
                                candle_interval: int = 900) -> dict[float, dict]:
    """Load OB snapshots aggregated to candle boundaries for backtest lookup.

    Raw OB snapshots have sub-second timestamps that won't match candle
    timestamps.  This aggregates in SQL using DISTINCT ON to grab the
    last snapshot per 15-min bucket, so we transfer only one row per
    candle instead of millions.
    """
    where_clause = "WHERE ticker = %s" if ticker else ""
    params: list = [ticker] if ticker else []

    query = f"""
        SELECT DISTINCT ON (bucket)
            EXTRACT(EPOCH FROM
                date_trunc('hour', timestamp)
                + INTERVAL '1 second' * (
                    FLOOR(EXTRACT(MINUTE FROM timestamp) / 15) * 15 * 60
                )
            ) AS bucket,
            obi,
            total_bid_vol,
            total_ask_vol,
            spread_cents
        FROM ob_snapshots
        {where_clause}
        ORDER BY bucket, timestamp DESC
    """

    async with pool.connection() as conn:
        rows = await conn.execute(query, params)
        result = await rows.fetchall()
        return {
            float(r[0]): {
                "bids": [],
                "asks": [],
                "obi": float(r[1]) if r[1] else 0.5,
                "total_bid_vol": float(r[2]) if r[2] else 0,
                "total_ask_vol": float(r[3]) if r[3] else 0,
                "spread_cents": float(r[4]) if r[4] is not None else None,
            }
            for r in result
        }


def _parse_book_levels(raw_levels) -> list[tuple[float, float]]:
    """Parse OB side JSON into [(price, size), ...]."""
    if raw_levels is None:
        return []
    levels = raw_levels
    if isinstance(levels, str):
        try:
            levels = json.loads(levels)
        except json.JSONDecodeError:
            return []
    out: list[tuple[float, float]] = []
    if not isinstance(levels, list):
        return out
    for row in levels:
        if isinstance(row, dict):
            price = row.get("price")
            size = row.get("size")
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            price, size = row[0], row[1]
        else:
            continue
        try:
            out.append((float(price), float(size)))
        except (TypeError, ValueError):
            continue
    return out


def derive_mid_from_book(
    bids_jsonb,
    asks_jsonb,
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Return (mid, best_bid, best_ask, spread) from JSONB book columns."""
    bids = _parse_book_levels(bids_jsonb)
    asks = _parse_book_levels(asks_jsonb)
    best_bid = max((p for p, s in bids if s > 0), default=None)
    best_ask = min((p for p, s in asks if s > 0), default=None)
    if best_bid is not None and best_ask is not None:
        spread = best_ask - best_bid
        return (best_bid + best_ask) / 2.0, best_bid, best_ask, spread
    if best_bid is not None:
        return best_bid, best_bid, None, None
    if best_ask is not None:
        return best_ask, None, best_ask, None
    return None, None, None, None


async def load_contract_timelines_db(
    pool,
    start_ts: float,
    end_ts: float,
    tickers: Optional[list[str]] = None,
    series: str = "KXBTC",
    bucket_sec: int = 0,
) -> dict[str, ContractTimeline]:
    """Load per-ticker contract timelines from OB snapshots + trade prints.

    Args:
        bucket_sec: If > 0, thin the OB data to one snapshot per
            ``bucket_sec``-second window per ticker (first row in bucket).
            Use 10-30 for multi-week sweep workloads that would OOM at full
            1-4s resolution.  Parity-replay and short windows should keep
            the default of 0 (full resolution).
    """
    start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)
    timelines: dict[str, ContractTimeline] = {}

    where_parts = ["timestamp >= %s", "timestamp <= %s"]
    params: list = [start_dt, end_dt]
    if tickers:
        placeholders = ",".join(["%s"] * len(tickers))
        where_parts.append(f"ticker IN ({placeholders})")
        params.extend(tickers)
    else:
        where_parts.append("ticker LIKE %s")
        params.append(f"{series}%")

    where_clause = " AND ".join(where_parts)
    if bucket_sec > 0:
        # Use TimescaleDB time_bucket + first() to downsample to one row per
        # bucket per ticker. This is far cheaper than ROW_NUMBER CTEs on large
        # hypertables because TimescaleDB can scan chunks in order and the
        # first() aggregate is optimised for ordered time-series data.
        interval_literal = f"{int(bucket_sec)} seconds"
        query = f"""
            SELECT
                time_bucket('{interval_literal}', timestamp) AS timestamp,
                ticker,
                first(bids,         timestamp) AS bids,
                first(asks,         timestamp) AS asks,
                first(obi,          timestamp) AS obi,
                first(total_bid_vol, timestamp) AS total_bid_vol,
                first(total_ask_vol, timestamp) AS total_ask_vol,
                first(spread_cents,  timestamp) AS spread_cents
            FROM ob_snapshots
            WHERE {where_clause}
            GROUP BY 1, 2
            ORDER BY timestamp ASC
        """
    else:
        query = f"""
            SELECT timestamp, ticker, bids, asks, obi, total_bid_vol, total_ask_vol, spread_cents
            FROM ob_snapshots
            WHERE {where_clause}
            ORDER BY timestamp ASC
        """

    async with pool.connection() as conn:
        rows = await conn.execute(query, params)
        result = await rows.fetchall()

    for row in result:
        ts = row[0].timestamp()
        ticker = row[1]
        mid, best_bid, best_ask, spread = derive_mid_from_book(row[2], row[3])
        spread_cents = float(row[7]) if row[7] is not None else spread
        timeline = timelines.setdefault(ticker, ContractTimeline(ticker=ticker))
        timeline.add_tick(
            ContractTick(
                timestamp=ts,
                ticker=ticker,
                mid_cents=mid,
                best_bid=best_bid,
                best_ask=best_ask,
                spread_cents=spread_cents,
                obi=float(row[4]) if row[4] is not None else 0.5,
                total_bid_vol=float(row[5]) if row[5] is not None else 0.0,
                total_ask_vol=float(row[6]) if row[6] is not None else 0.0,
                source="ob_mid",
            )
        )

    trades_where_parts = ["created_time >= %s", "created_time <= %s"]
    trades_params: list = [start_dt, end_dt]
    if tickers:
        placeholders = ",".join(["%s"] * len(tickers))
        trades_where_parts.append(f"ticker IN ({placeholders})")
        trades_params.extend(tickers)
    else:
        trades_where_parts.append("ticker LIKE %s")
        trades_params.append(f"{series}%")

    trades_query = f"""
        SELECT created_time, ticker, yes_price
        FROM kalshi_trades
        WHERE {" AND ".join(trades_where_parts)}
        ORDER BY created_time ASC
    """
    async with pool.connection() as conn:
        rows = await conn.execute(trades_query, trades_params)
        trades = await rows.fetchall()

    for row in trades:
        if row[2] is None:
            continue
        ts = row[0].timestamp()
        ticker = row[1]
        yes_price = float(row[2])
        timeline = timelines.setdefault(ticker, ContractTimeline(ticker=ticker))
        timeline.add_tick(
            ContractTick(
                timestamp=ts,
                ticker=ticker,
                mid_cents=yes_price,
                best_bid=None,
                best_ask=None,
                spread_cents=None,
                obi=0.5,
                total_bid_vol=0.0,
                total_ask_vol=0.0,
                source="yes_price",
            )
        )

    for timeline in timelines.values():
        timeline.finalize()
    return timelines


async def load_settlement_outcomes_db(
    pool,
    tickers: Optional[list[str]] = None,
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
    series: str = "KXBTC",
) -> dict[str, dict]:
    """Load settled contract outcomes keyed by ticker."""
    where_parts = ["result IS NOT NULL"]
    params: list = []
    if start_ts is not None:
        where_parts.append("close_time >= %s")
        params.append(datetime.fromtimestamp(start_ts, tz=timezone.utc))
    if end_ts is not None:
        where_parts.append("close_time <= %s")
        params.append(datetime.fromtimestamp(end_ts, tz=timezone.utc))
    if tickers:
        placeholders = ",".join(["%s"] * len(tickers))
        where_parts.append(f"ticker IN ({placeholders})")
        params.extend(tickers)
    else:
        where_parts.append("ticker LIKE %s")
        params.append(f"{series}%")

    query = f"""
        SELECT ticker, close_time, result, expiration_value, last_price, volume
        FROM kalshi_markets
        WHERE {" AND ".join(where_parts)}
        ORDER BY close_time ASC
    """

    async with pool.connection() as conn:
        rows = await conn.execute(query, params)
        result = await rows.fetchall()
        return {
            r[0]: {
                "close_time": r[1].timestamp() if r[1] else None,
                "result": r[2],
                "expiration_value": float(r[3]) if r[3] is not None else None,
                "last_price": float(r[4]) if r[4] is not None else None,
                "volume": float(r[5]) if r[5] is not None else None,
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


async def load_kalshi_markets_db(
    pool,
    series: str = "KXBTC",
    limit: int = 5000,
) -> dict[str, dict]:
    """Load settled market metadata keyed by ticker.
    Used by backtester to get settlement price for exit accuracy.
    """
    async with pool.connection() as conn:
        rows = await conn.execute(
            """SELECT ticker, close_time, result, expiration_value,
                      last_price, volume
               FROM kalshi_markets
               WHERE ticker LIKE %s
               ORDER BY close_time ASC
               LIMIT %s""",
            (f"{series}%", limit)
        )
        result = await rows.fetchall()
        return {
            r[0]: {
                "close_time": r[1].timestamp() if r[1] else None,
                "result": r[2],
                "expiration_value": float(r[3]) if r[3] else None,
                "last_price": float(r[4]) if r[4] else None,
                "volume": float(r[5]) if r[5] else None,
            }
            for r in result
        }


async def load_tfi_history_db(
    pool,
    ticker: Optional[str] = None,
    window_minutes: int = 15,
    limit: int = 100000,
) -> dict[float, float]:
    """Load pre-computed rolling TFI keyed by timestamp (seconds).
    Used by backtester to inject TFI as a second OBI feature.
    """
    query = """
        SELECT
            EXTRACT(EPOCH FROM
                date_trunc('minute', created_time)
            ) AS ts,
            SUM(CASE WHEN taker_side = 'yes' THEN count_fp ELSE 0 END)
                / NULLIF(SUM(count_fp), 0) AS tfi
        FROM kalshi_trades
    """
    params: list = []
    if ticker:
        query += " WHERE ticker = %s"
        params.append(ticker)
    query += " GROUP BY 1 ORDER BY 1 ASC LIMIT %s"
    params.append(limit)

    async with pool.connection() as conn:
        rows = await conn.execute(query, params)
        result = await rows.fetchall()
        return {float(r[0]): float(r[1]) for r in result if r[1] is not None}


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
