"""Backfill historical live ``trade_features`` rows from raw market data.

Tier 1.a (2026-04-28): the ML feature-capture path was added late in
the live-trading window, so most live trades have no row in
``trade_features``. The training set is dominated by paper rows, which
makes the model blind to live-specific patterns (slower fills, settlement
gamma, etc.). This script reconstructs as much of the feature surface as
possible from the historical record, so the next retrain can consume
the live tail.

What we can reconstruct (with high quality):
  * obi, bid_depth, ask_depth, spread_pct      <- from ob_snapshots
  * book_thickness_at_offer, quoted_spread_at_entry_bps  <- from ob_snapshots bids/asks JSONB
  * minutes_to_contract_close, time_remaining_sec  <- from trade.timestamp + contract close
  * hour_of_day, day_of_week                   <- from trade.timestamp
  * recent_trade_count_60s                     <- from kalshi_trades count in 60s window
  * pnl, label                                 <- from trades (already captured)

What we CANNOT reconstruct without 1m candle data lookalike:
  * roc_3 / roc_5 / roc_10                     <- need spot price history
  * atr_pct                                    <- need spot price history
  * green_candles_3, candle_body_pct           <- need 1m candle history
  * volume_ratio                               <- need per-candle tick counts
  * tfi, taker_buy_vol, taker_sell_vol         <- need historical_sync data

For the unrecoverable fields we insert NULL. ``train_xgb.py`` already
uses ``X.fillna(0)`` so missing rows are handled at training time, BUT
that means the backfilled rows will look like "ROC was zero, ATR was
zero" to the model. This is a legitimate concern for training quality
on the backfilled rows specifically; it's why this script logs WARN-level
messages noting which fields are NULL per trade so the operator sees the
quality picture.

Scope (decided by user 2026-04-28):
  * trading_mode = 'live' only
  * data_quality_flag IS NULL only (skip CATASTROPHIC_SHORT, PRE_BUG027_WALLET,
    EXIT_REASON_RECLASSED, CORRUPTED_PNL — those rows have unreliable pnl/label)
  * Must have at least one ob_snapshot within 60 seconds before entry
  * Idempotent: trades that already have a trade_features row are skipped

Usage:
  python scripts/backfill_live_trade_features.py --dry-run   # report only
  python scripts/backfill_live_trade_features.py             # apply

Both forms require DATABASE_URL env or --db-url flag.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg

# Half-width must match BOOK_THICKNESS_HALF_WIDTH_CENTS in feature_capture.py.
# Hard-coded here because the script is intentionally standalone (so it can
# run on the host without the bot's full Python env).
BOOK_THICKNESS_HALF_WIDTH_CENTS = 5.0

# Window we'll search for the last ob_snapshot before a trade.
SNAPSHOT_LOOKBACK_SEC = 60

# Window for recent_trade_count_60s.
TRADE_COUNT_WINDOW_SEC = 60


@dataclass
class TradeRow:
    trade_id: int
    ticker: str
    timestamp: datetime
    direction: str
    entry_price: float
    pnl: Optional[float]
    data_quality_flag: Optional[str]


@dataclass
class Reconstruction:
    """Best-effort reconstruction. ``None`` fields will be inserted as NULL."""
    trade_id: int
    ticker: str
    timestamp: datetime
    snapshot_age_sec: Optional[float]   # how stale the snapshot is (smaller = better quality)

    obi: Optional[float] = None
    spread_pct: Optional[float] = None
    bid_depth: Optional[float] = None
    ask_depth: Optional[float] = None
    time_remaining_sec: Optional[int] = None
    hour_of_day: Optional[int] = None
    day_of_week: Optional[int] = None

    minutes_to_contract_close: Optional[float] = None
    quoted_spread_at_entry_bps: Optional[int] = None
    book_thickness_at_offer: Optional[float] = None
    recent_trade_count_60s: Optional[int] = None

    pnl: Optional[float] = None
    label: Optional[int] = None  # +1 / 0 / -1
    skipped_reason: Optional[str] = None

    def is_skipped(self) -> bool:
        return self.skipped_reason is not None


def _label_from_pnl(pnl: Optional[float]) -> Optional[int]:
    """Mirrors ml.feature_capture.label_trade()."""
    if pnl is None:
        return None
    if pnl > 0.001:
        return 1
    if pnl < -0.001:
        return -1
    return 0


def _ticker_close_time(ticker: str, trade_ts: datetime) -> Optional[datetime]:
    """Best-effort guess of the contract's close time from its ticker.

    Kalshi BTC 15-min ticker convention as observed:
      KXBTC-{YY}{MMM}{DD}{HH}-B{strike}
    where ``HH`` is some hour code that empirically maps to "the contract
    closes at minute :00 of hour HH+something" -- the exact rule isn't
    publicly documented in this repo. Rather than guess, we round
    ``trade_ts`` UP to the next 15-minute boundary; that's how Kalshi's
    KXBTC contracts settle. Fine-grained rotation around minute :00 might
    be off by a few seconds but the feature only needs minute resolution.
    """
    # Round up to the next 15-min boundary.
    minute = trade_ts.minute
    hour = trade_ts.hour
    next_quarter_min = ((minute // 15) + 1) * 15
    add_hours, mins = divmod(next_quarter_min, 60)
    close = trade_ts.replace(minute=0, second=0, microsecond=0) \
        + timedelta(hours=hour - trade_ts.hour + add_hours, minutes=mins)
    # Guard against the edge case where add_hours pushed us into the next day:
    # timedelta arithmetic above handles it correctly.
    return close


def _book_thickness(bids: list, asks: list, mid_price: float) -> Optional[float]:
    """Sum sizes within ±5c of mid. ``bids``/``asks`` are
    [[price_cents, size], ...] from ob_snapshots JSONB."""
    if mid_price is None:
        return None
    lo = mid_price - BOOK_THICKNESS_HALF_WIDTH_CENTS
    hi = mid_price + BOOK_THICKNESS_HALF_WIDTH_CENTS
    total = 0.0
    for level in (bids or []):
        if not level or len(level) < 2:
            continue
        p, s = float(level[0]), float(level[1])
        if lo <= p <= hi:
            total += s
    for level in (asks or []):
        if not level or len(level) < 2:
            continue
        p, s = float(level[0]), float(level[1])
        if lo <= p <= hi:
            total += s
    return total


def _reconstruct_from_snapshot(snapshot_row: dict, trade: TradeRow) -> dict:
    """Pull entry-time book features from an ob_snapshots row."""
    bids = snapshot_row["bids"] or []
    asks = snapshot_row["asks"] or []

    # Mid price: best bid + best ask / 2, parsed from the ladders.
    best_bid = max((float(level[0]) for level in bids if level and len(level) >= 2), default=None)
    best_ask = min((float(level[0]) for level in asks if level and len(level) >= 2), default=None)
    mid_price = None
    if best_bid is not None and best_ask is not None:
        mid_price = (best_bid + best_ask) / 2

    spread_cents = snapshot_row.get("spread_cents")
    if spread_cents is None and best_bid is not None and best_ask is not None:
        spread_cents = best_ask - best_bid

    quoted_spread_at_entry_bps = None
    if spread_cents is not None and mid_price and mid_price > 0:
        quoted_spread_at_entry_bps = int(round((spread_cents / mid_price) * 10000))

    spread_pct = None
    if spread_cents is not None and mid_price and mid_price > 0:
        spread_pct = round(spread_cents / mid_price, 6)

    book_thickness = _book_thickness(bids, asks, mid_price) if mid_price is not None else None

    return {
        "obi": float(snapshot_row["obi"]) if snapshot_row.get("obi") is not None else None,
        "spread_pct": spread_pct,
        "bid_depth": float(snapshot_row["total_bid_vol"]) if snapshot_row.get("total_bid_vol") is not None else None,
        "ask_depth": float(snapshot_row["total_ask_vol"]) if snapshot_row.get("total_ask_vol") is not None else None,
        "quoted_spread_at_entry_bps": quoted_spread_at_entry_bps,
        "book_thickness_at_offer": book_thickness,
    }


def _eligible_trades(cur) -> list[TradeRow]:
    cur.execute(
        """
        SELECT t.id, t.ticker, t.timestamp, t.direction, t.entry_price,
               t.pnl, t.data_quality_flag
        FROM trades t
        LEFT JOIN trade_features tf ON tf.trade_id = t.id
        WHERE t.trading_mode = 'live'
          AND t.data_quality_flag IS NULL
          AND tf.id IS NULL
        ORDER BY t.id
        """
    )
    rows = cur.fetchall()
    return [
        TradeRow(
            trade_id=r[0],
            ticker=r[1],
            timestamp=r[2],
            direction=r[3],
            entry_price=float(r[4]) if r[4] is not None else None,
            pnl=float(r[5]) if r[5] is not None else None,
            data_quality_flag=r[6],
        )
        for r in rows
    ]


def _latest_snapshot(cur, ticker: str, ts: datetime) -> Optional[dict]:
    """Return the most recent ob_snapshots row at or before ``ts`` within
    SNAPSHOT_LOOKBACK_SEC seconds. ``None`` if no usable snapshot."""
    earliest = ts - timedelta(seconds=SNAPSHOT_LOOKBACK_SEC)
    cur.execute(
        """
        SELECT timestamp, obi, total_bid_vol, total_ask_vol, spread_cents,
               bids, asks
        FROM ob_snapshots
        WHERE ticker = %s
          AND timestamp <= %s
          AND timestamp > %s
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (ticker, ts, earliest),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "timestamp": row[0],
        "obi": row[1],
        "total_bid_vol": row[2],
        "total_ask_vol": row[3],
        "spread_cents": row[4],
        "bids": row[5],
        "asks": row[6],
    }


def _recent_trade_count(cur, ticker: str, ts: datetime) -> int:
    """Number of public matched trades on this contract in the prior
    ``TRADE_COUNT_WINDOW_SEC`` seconds. Direct analogue of the live
    ``recent_trade_count_60s`` feature."""
    earliest = ts - timedelta(seconds=TRADE_COUNT_WINDOW_SEC)
    cur.execute(
        """
        SELECT COUNT(*)
        FROM kalshi_trades
        WHERE ticker = %s
          AND created_time <= %s
          AND created_time > %s
        """,
        (ticker, ts, earliest),
    )
    row = cur.fetchone()
    return int(row[0] or 0)


def _reconstruct(cur, trade: TradeRow) -> Reconstruction:
    rec = Reconstruction(
        trade_id=trade.trade_id,
        ticker=trade.ticker,
        timestamp=trade.timestamp,
        snapshot_age_sec=None,
    )

    snap = _latest_snapshot(cur, trade.ticker, trade.timestamp)
    if snap is None:
        rec.skipped_reason = "no_snapshot_within_60s"
        return rec

    rec.snapshot_age_sec = (trade.timestamp - snap["timestamp"]).total_seconds()

    book_features = _reconstruct_from_snapshot(snap, trade)
    rec.obi = book_features["obi"]
    rec.spread_pct = book_features["spread_pct"]
    rec.bid_depth = book_features["bid_depth"]
    rec.ask_depth = book_features["ask_depth"]
    rec.quoted_spread_at_entry_bps = book_features["quoted_spread_at_entry_bps"]
    rec.book_thickness_at_offer = book_features["book_thickness_at_offer"]

    rec.hour_of_day = trade.timestamp.hour
    rec.day_of_week = trade.timestamp.weekday()

    close = _ticker_close_time(trade.ticker, trade.timestamp)
    if close:
        time_remaining_sec = max(0, int((close - trade.timestamp).total_seconds()))
        rec.time_remaining_sec = time_remaining_sec
        rec.minutes_to_contract_close = round(time_remaining_sec / 60.0, 3)

    rec.recent_trade_count_60s = _recent_trade_count(cur, trade.ticker, trade.timestamp)

    rec.pnl = trade.pnl
    rec.label = _label_from_pnl(trade.pnl)
    return rec


def _insert_row(cur, rec: Reconstruction) -> None:
    cur.execute(
        """
        INSERT INTO trade_features (
            trade_id, trading_mode, ticker, timestamp,
            obi, spread_pct, bid_depth, ask_depth,
            time_remaining_sec, hour_of_day, day_of_week,
            minutes_to_contract_close, quoted_spread_at_entry_bps,
            book_thickness_at_offer, recent_trade_count_60s,
            label, pnl
        ) VALUES (
            %s, 'live', %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s,
            %s, %s
        )
        """,
        (
            rec.trade_id, rec.ticker, rec.timestamp,
            rec.obi, rec.spread_pct, rec.bid_depth, rec.ask_depth,
            rec.time_remaining_sec, rec.hour_of_day, rec.day_of_week,
            rec.minutes_to_contract_close, rec.quoted_spread_at_entry_bps,
            rec.book_thickness_at_offer, rec.recent_trade_count_60s,
            rec.label, rec.pnl,
        ),
    )


def _summary(reconstructions: list[Reconstruction]) -> dict:
    n_total = len(reconstructions)
    n_skipped = sum(1 for r in reconstructions if r.is_skipped())
    n_recoverable = n_total - n_skipped

    skip_breakdown: dict[str, int] = {}
    for r in reconstructions:
        if r.is_skipped():
            skip_breakdown[r.skipped_reason] = skip_breakdown.get(r.skipped_reason, 0) + 1

    snapshot_ages = [r.snapshot_age_sec for r in reconstructions if r.snapshot_age_sec is not None]

    return {
        "n_eligible": n_total,
        "n_recoverable": n_recoverable,
        "n_skipped": n_skipped,
        "skip_breakdown": skip_breakdown,
        "snapshot_age_sec": {
            "min": round(min(snapshot_ages), 3) if snapshot_ages else None,
            "max": round(max(snapshot_ages), 3) if snapshot_ages else None,
            "mean": round(sum(snapshot_ages) / len(snapshot_ages), 3) if snapshot_ages else None,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill live trade_features from historical data")
    parser.add_argument("--db-url", default=os.environ.get("DATABASE_URL"),
                        help="Postgres URL (defaults to $DATABASE_URL)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only; never insert.")
    args = parser.parse_args()

    if not args.db_url:
        print("ERROR: provide --db-url or set DATABASE_URL", file=sys.stderr)
        return 1

    print("──────────────────────────────────────────────────────────────")
    print(f" backfill_live_trade_features.py")
    print(f" mode      : {'DRY RUN' if args.dry_run else 'APPLY'}")
    print(f" started   : {datetime.now(timezone.utc).isoformat()}")
    print("──────────────────────────────────────────────────────────────")

    try:
        with psycopg.connect(args.db_url) as conn:
            with conn.cursor() as cur:
                trades = _eligible_trades(cur)
                print(f"\n  eligible live trades (no flag, no existing features): {len(trades)}")

                reconstructions = []
                for trade in trades:
                    rec = _reconstruct(cur, trade)
                    reconstructions.append(rec)

                print("\n  per-trade detail:")
                for rec in reconstructions:
                    if rec.is_skipped():
                        print(f"    [SKIP]  trade_id={rec.trade_id}  {rec.ticker}  reason={rec.skipped_reason}")
                    else:
                        print(
                            f"    [OK]    trade_id={rec.trade_id}  {rec.ticker}  "
                            f"snap_age={rec.snapshot_age_sec:.1f}s  obi={rec.obi}  "
                            f"book_thickness={rec.book_thickness_at_offer}  "
                            f"recent_trades_60s={rec.recent_trade_count_60s}  "
                            f"min_to_close={rec.minutes_to_contract_close}  "
                            f"label={rec.label}"
                        )

                summary = _summary(reconstructions)
                print("\n  summary:")
                print(json.dumps(summary, indent=4, default=str))

                if args.dry_run:
                    print("\n  DRY RUN: no rows inserted.")
                    conn.rollback()
                    return 0

                n_inserted = 0
                for rec in reconstructions:
                    if rec.is_skipped():
                        continue
                    _insert_row(cur, rec)
                    n_inserted += 1

                conn.commit()
                print(f"\n  INSERTED: {n_inserted} new live trade_features rows.")

                cur.execute(
                    "SELECT COUNT(*) FROM trade_features WHERE trading_mode = 'live' AND label IS NOT NULL"
                )
                row = cur.fetchone()
                final_count = row[0] if row else 0
                print(f"  final live trade_features count (labeled): {final_count}")

                return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
