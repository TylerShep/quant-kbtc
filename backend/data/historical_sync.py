"""
Historical data sync orchestrator.
Bootstraps and continuously syncs:
  1. Predexon L2 OB snapshots -> ob_snapshots
  2. Kalshi settled markets   -> kalshi_markets
  3. Kalshi public trades     -> kalshi_trades + TFI cache

All tasks are fire-and-forget asyncio loops. Errors are logged
and reported via Discord notifier but never propagate to caller.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

import structlog

from config import settings

logger = structlog.get_logger(__name__)

BATCH_SIZE = 500


class HistoricalSync:
    """
    Manages all three historical data pipelines.
    Instantiated once and wired into Coordinator.start().
    """

    def __init__(self):
        self._pool = None
        self._tfi_cache: dict[str, dict] = {}
        self._settlement_cursor: Optional[str] = None

    async def start(self, pool) -> None:
        """Called by coordinator.start(). Starts all sync loops."""
        cfg = settings.historical_sync
        if not cfg.enabled:
            logger.info("historical_sync.disabled")
            return
        self._pool = pool
        await self._run_migration()
        asyncio.create_task(self._settlement_sync_loop())
        asyncio.create_task(self._trades_sync_loop())
        asyncio.create_task(self._ob_bootstrap_loop())
        logger.info("historical_sync.started")

    # ─── Migration ───────────────────────────────────────────────────

    async def _run_migration(self) -> None:
        """Apply schema additions idempotently, one statement at a time."""
        from pathlib import Path
        sql_path = Path(__file__).parent.parent / "migrations" / "002_historical_data.sql"
        if not sql_path.exists():
            logger.warning("historical_sync.migration_sql_missing", path=str(sql_path))
            return
        sql = sql_path.read_text()
        raw = [s.strip() for s in sql.split(";\n") if s.strip() and not s.strip().startswith("--")]
        statements = [s.rstrip(";") for s in raw]
        errors = 0
        for stmt in statements:
            try:
                async with self._pool.connection() as conn:
                    await conn.set_autocommit(True)
                    await conn.execute(stmt)
            except Exception as e:
                errors += 1
                logger.warning("historical_sync.migration_stmt_failed",
                               stmt=stmt[:80], error=str(e))
        if errors == 0:
            logger.info("historical_sync.migration_complete")
        else:
            logger.info("historical_sync.migration_partial",
                        errors=errors, total=len(statements))

    # ─── Enhancement 1: Predexon OB Bootstrap ────────────────────────

    async def _ob_bootstrap_loop(self) -> None:
        """Bootstrap + keep-fresh ob_snapshots from Predexon."""
        cfg = settings.historical_sync
        first = True
        while True:
            try:
                await self._sync_ob_snapshots()
                if first:
                    first = False
                    logger.info("historical_sync.ob_bootstrap_complete")
            except Exception as e:
                await self._notify_error("ob_bootstrap", str(e))
            await asyncio.sleep(60 if first else cfg.predexon_interval_sec)

    async def _sync_ob_snapshots(self) -> None:
        cfg = settings.historical_sync
        if not cfg.predexon_api_key:
            logger.warning("historical_sync.predexon_key_missing")
            return

        from data.predexon import PredexonClient
        client = PredexonClient()

        newest = await self._newest_ob_ts()
        if newest:
            min_ts = newest
            logger.info("historical_sync.ob_incremental", since=str(min_ts))
        else:
            min_ts = datetime.now(timezone.utc) - timedelta(days=cfg.predexon_bootstrap_days)
            logger.info("historical_sync.ob_full_bootstrap", days=cfg.predexon_bootstrap_days)

        tickers = await self._kxbtc_tickers_since(min_ts)
        if not tickers:
            logger.info("historical_sync.ob_no_tickers")
            return

        inserted = 0
        batch: list[tuple] = []
        for ticker in tickers:
            async for snap in client.iter_ob_snapshots(ticker, min_ts=min_ts):
                row = self._parse_ob_row(ticker, snap)
                if row:
                    batch.append(row)
                if len(batch) >= BATCH_SIZE:
                    inserted += await self._flush_ob_batch(batch)
                    batch.clear()
        if batch:
            inserted += await self._flush_ob_batch(batch)
        logger.info("historical_sync.ob_snapshots_inserted", count=inserted)

    def _parse_ob_row(self, ticker: str, snap: dict) -> Optional[tuple]:
        """Parse a Predexon snapshot into a DB row tuple."""
        try:
            from data.predexon import PredexonClient
            obi = PredexonClient.compute_obi(snap)
            ts = datetime.fromtimestamp(snap["timestamp"], tz=timezone.utc)
            bid_depth = snap.get("bid_depth", 0) or 0
            ask_depth = snap.get("ask_depth", 0) or 0
            spread = None
            if snap.get("best_ask") and snap.get("best_bid"):
                spread = int(round((snap["best_ask"] - snap["best_bid"]) * 100))
            bids_json = json.dumps(snap.get("yes_bids", []))
            asks_json = json.dumps(snap.get("yes_asks", []))
            return (ts, ticker, bids_json, asks_json, obi,
                    bid_depth, ask_depth, spread)
        except Exception:
            return None

    async def _flush_ob_batch(self, batch: list[tuple]) -> int:
        """Insert a batch of OB snapshot rows."""
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.executemany(
                        """INSERT INTO ob_snapshots
                           (timestamp, ticker, bids, asks, obi,
                            total_bid_vol, total_ask_vol, spread_cents)
                           VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s)
                           ON CONFLICT (ticker, timestamp) DO NOTHING""",
                        batch,
                    )
            return len(batch)
        except Exception as e:
            logger.error("historical_sync.ob_batch_failed", error=str(e), count=len(batch))
            return 0

    async def _newest_ob_ts(self) -> Optional[datetime]:
        try:
            async with self._pool.connection() as conn:
                row = await conn.execute(
                    "SELECT MAX(timestamp) FROM ob_snapshots"
                )
                result = await row.fetchone()
                if result and result[0]:
                    return result[0].replace(tzinfo=timezone.utc)
        except Exception:
            pass
        return None

    async def _kxbtc_tickers_since(self, since: datetime) -> list[str]:
        try:
            async with self._pool.connection() as conn:
                rows = await conn.execute(
                    "SELECT ticker FROM kalshi_markets WHERE close_time >= %s ORDER BY close_time ASC",
                    (since,)
                )
                return [r[0] for r in await rows.fetchall()]
        except Exception:
            return []

    # ─── Enhancement 2: Settlement Sync ──────────────────────────────

    async def _settlement_sync_loop(self) -> None:
        """Backfill + keep-fresh kalshi_markets from Kalshi historical API."""
        cfg = settings.historical_sync
        first = True
        while True:
            try:
                await self._sync_settlements()
                if first:
                    first = False
                    logger.info("historical_sync.settlements_complete")
            except Exception as e:
                await self._notify_error("settlement_sync", str(e))
            await asyncio.sleep(cfg.settlement_interval_sec)

    async def _sync_settlements(self) -> None:
        from data.kalshi_rest import KalshiHistoricalClient
        client = KalshiHistoricalClient()

        resume = self._settlement_cursor or await self._load_settlement_cursor()

        inserted = 0
        batch: list[tuple] = []
        last_cursor = resume
        async for market, cursor in client.iter_historical_markets(resume_cursor=resume):
            last_cursor = cursor
            row = self._parse_settlement_row(market)
            if row:
                batch.append(row)
            if len(batch) >= BATCH_SIZE:
                inserted += await self._flush_settlement_batch(batch)
                batch.clear()
                await self._save_settlement_cursor(last_cursor)
        if batch:
            inserted += await self._flush_settlement_batch(batch)
        if last_cursor:
            self._settlement_cursor = last_cursor
            await self._save_settlement_cursor(last_cursor)
        if not last_cursor:
            self._settlement_cursor = None
            await self._save_settlement_cursor(None)
        logger.info("historical_sync.settlements_inserted", count=inserted,
                     has_more=bool(last_cursor))

    def _parse_settlement_row(self, m: dict) -> Optional[tuple]:
        """Parse a Kalshi market dict into a DB row tuple."""
        ticker = m.get("ticker", "")
        if "KXBTC" not in ticker:
            return None
        close_time_str = m.get("close_time") or m.get("latest_expiration_time")
        if not close_time_str:
            return None
        try:
            close_time = datetime.fromisoformat(
                close_time_str.replace("Z", "+00:00")
            )
        except ValueError:
            return None
        open_time = None
        if m.get("open_time"):
            try:
                open_time = datetime.fromisoformat(
                    m["open_time"].replace("Z", "+00:00")
                )
            except ValueError:
                pass
        exp_val = None
        if m.get("expiration_value"):
            try:
                exp_val = float(m["expiration_value"])
            except (ValueError, TypeError):
                pass
        last_price = None
        for field in ("last_price_dollars", "last_price", "yes_bid_dollars"):
            if m.get(field):
                try:
                    last_price = float(m[field])
                    break
                except (ValueError, TypeError):
                    pass
        vol = None
        for field in ("volume_fp", "volume"):
            if m.get(field):
                try:
                    vol = float(m[field])
                    break
                except (ValueError, TypeError):
                    pass
        return (ticker, m.get("event_ticker"), open_time, close_time,
                m.get("result"), exp_val, last_price, vol)

    async def _flush_settlement_batch(self, batch: list[tuple]) -> int:
        """Insert a batch of settlement rows."""
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.executemany(
                        """INSERT INTO kalshi_markets
                           (ticker, event_ticker, open_time, close_time,
                            result, expiration_value, last_price, volume, source)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'historical')
                           ON CONFLICT (ticker) DO UPDATE
                             SET result = EXCLUDED.result,
                                 expiration_value = EXCLUDED.expiration_value,
                                 last_price = EXCLUDED.last_price,
                                 volume = EXCLUDED.volume,
                                 fetched_at = NOW()""",
                        batch,
                    )
            return len(batch)
        except Exception as e:
            logger.error("historical_sync.settlement_batch_failed",
                         error=str(e), count=len(batch))
            return 0

    async def _load_settlement_cursor(self) -> Optional[str]:
        """Load the last saved cursor from bot_state for resumable sync."""
        try:
            async with self._pool.connection() as conn:
                row = await conn.execute(
                    "SELECT value FROM bot_state WHERE key = 'settlement_sync_cursor'"
                )
                result = await row.fetchone()
                if result and result[0]:
                    val = result[0]
                    if isinstance(val, dict):
                        return val.get("cursor")
                    return str(val).strip('"')
        except Exception:
            pass
        return None

    async def _save_settlement_cursor(self, cursor: Optional[str]) -> None:
        """Persist the cursor so sync can resume after restart."""
        try:
            async with self._pool.connection() as conn:
                if cursor:
                    cursor_json = json.dumps({"cursor": cursor})
                    await conn.execute(
                        """INSERT INTO bot_state (key, value) VALUES ('settlement_sync_cursor', %s::jsonb)
                           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                        (cursor_json,)
                    )
                else:
                    await conn.execute(
                        "DELETE FROM bot_state WHERE key = 'settlement_sync_cursor'"
                    )
        except Exception as e:
            logger.warning("historical_sync.cursor_save_failed", error=str(e))

    # ─── Enhancement 3: Public Trades + TFI ──────────────────────────

    async def _trades_sync_loop(self) -> None:
        """Sync kalshi_trades and maintain in-memory TFI cache."""
        cfg = settings.historical_sync
        first = True
        while True:
            try:
                await self._sync_kalshi_trades()
                await self._rebuild_tfi_cache()
                if first:
                    first = False
                    logger.info("historical_sync.trades_complete")
            except Exception as e:
                await self._notify_error("trades_sync", str(e))
            await asyncio.sleep(cfg.trades_interval_sec)

    async def _sync_kalshi_trades(self) -> None:
        from data.kalshi_rest import KalshiHistoricalClient
        client = KalshiHistoricalClient()

        cfg = settings.historical_sync
        tfi_window = datetime.now(timezone.utc) - timedelta(minutes=cfg.tfi_window_minutes)

        newest = await self._newest_trade_ts()
        if newest:
            min_ts = max(newest - timedelta(minutes=5), tfi_window)
        else:
            min_ts = tfi_window

        active_tickers = await client.get_active_tickers()
        if not active_tickers:
            logger.info("historical_sync.trades_no_active_tickers")
            return

        inserted = 0
        batch: list[tuple] = []
        for ticker in active_tickers:
            async for trade in client.iter_live_trades(
                ticker=ticker, min_ts=min_ts, max_pages=10
            ):
                row = self._parse_trade_row(trade)
                if row:
                    batch.append(row)
                if len(batch) >= BATCH_SIZE:
                    inserted += await self._flush_trade_batch(batch)
                    batch.clear()
        if batch:
            inserted += await self._flush_trade_batch(batch)
        if inserted:
            logger.info("historical_sync.trades_inserted", count=inserted)

    def _parse_trade_row(self, t: dict) -> Optional[tuple]:
        """Parse a Kalshi trade dict into a DB row tuple."""
        ticker = t.get("ticker", "")
        if "KXBTC" not in ticker:
            return None
        created_str = t.get("created_time", "")
        if not created_str:
            return None
        try:
            created_time = datetime.fromisoformat(
                created_str.replace("Z", "+00:00")
            )
        except ValueError:
            return None
        yes_price = None
        for f in ("yes_price_dollars", "yes_price"):
            if t.get(f):
                try:
                    yes_price = float(t[f])
                    break
                except (ValueError, TypeError):
                    pass
        count = None
        if t.get("count_fp"):
            try:
                count = float(t["count_fp"])
            except (ValueError, TypeError):
                pass
        return (t.get("trade_id", ""), ticker, count,
                yes_price, t.get("taker_side"), created_time)

    async def _flush_trade_batch(self, batch: list[tuple]) -> int:
        """Insert a batch of trade rows."""
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.executemany(
                        """INSERT INTO kalshi_trades
                           (trade_id, ticker, count_fp, yes_price,
                            taker_side, created_time)
                           VALUES (%s, %s, %s, %s, %s, %s)
                           ON CONFLICT (trade_id, created_time) DO NOTHING""",
                        batch,
                    )
            return len(batch)
        except Exception as e:
            logger.error("historical_sync.trade_batch_failed",
                         error=str(e), count=len(batch))
            return 0

    async def _newest_trade_ts(self) -> Optional[datetime]:
        try:
            async with self._pool.connection() as conn:
                row = await conn.execute(
                    "SELECT MAX(created_time) FROM kalshi_trades"
                )
                result = await row.fetchone()
                if result and result[0]:
                    return result[0].replace(tzinfo=timezone.utc)
        except Exception:
            pass
        return None

    async def _active_kxbtc_tickers(self) -> list[str]:
        """Return distinct tickers from kalshi_markets in the sync window."""
        cfg = settings.historical_sync
        cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.trades_sync_days)
        try:
            async with self._pool.connection() as conn:
                rows = await conn.execute(
                    "SELECT ticker FROM kalshi_markets WHERE close_time >= %s ORDER BY close_time DESC",
                    (cutoff,)
                )
                return [r[0] for r in await rows.fetchall()]
        except Exception:
            return []

    # ─── TFI Cache ────────────────────────────────────────────────────

    async def _rebuild_tfi_cache(self) -> None:
        """Recompute TFI for active tickers from DB trade data."""
        cfg = settings.historical_sync
        window_start = datetime.now(timezone.utc) - timedelta(minutes=cfg.tfi_window_minutes)

        from data.kalshi_rest import KalshiHistoricalClient
        tickers = await KalshiHistoricalClient().get_active_tickers()
        if not tickers:
            tickers = await self._active_kxbtc_tickers()

        new_cache: dict[str, dict] = {}
        for ticker in tickers:
            try:
                async with self._pool.connection() as conn:
                    rows = await conn.execute(
                        """SELECT taker_side, SUM(count_fp) as vol
                           FROM kalshi_trades
                           WHERE ticker = %s AND created_time >= %s
                           GROUP BY taker_side""",
                        (ticker, window_start)
                    )
                    result = await rows.fetchall()
                buy_vol = sum(float(r[1]) for r in result if r[0] == "yes")
                sell_vol = sum(float(r[1]) for r in result if r[0] == "no")
                if buy_vol + sell_vol > 0:
                    new_cache[ticker] = {
                        "buy_vol": buy_vol,
                        "sell_vol": sell_vol,
                        "window_start": window_start,
                    }
            except Exception:
                pass
        self._tfi_cache = new_cache

    def get_tfi(self, ticker: str) -> Optional[float]:
        """
        Return trade flow imbalance for a ticker.
        TFI = buy_vol / (buy_vol + sell_vol), range [0,1].
        Returns None if no data in cache.
        """
        entry = self._tfi_cache.get(ticker)
        if not entry:
            return None
        total = entry["buy_vol"] + entry["sell_vol"]
        if total == 0:
            return None
        return entry["buy_vol"] / total

    def get_tfi_volumes(self, ticker: str) -> tuple[Optional[float], Optional[float]]:
        """Return (buy_vol, sell_vol) for a ticker, or (None, None)."""
        entry = self._tfi_cache.get(ticker)
        if not entry:
            return None, None
        return entry["buy_vol"], entry["sell_vol"]

    async def get_settlement_price(self, ticker: str) -> Optional[float]:
        """Return expiration_value for a settled ticker."""
        try:
            async with self._pool.connection() as conn:
                row = await conn.execute(
                    "SELECT expiration_value FROM kalshi_markets WHERE ticker = %s",
                    (ticker,)
                )
                result = await row.fetchone()
                if result and result[0]:
                    return float(result[0])
        except Exception:
            pass
        return None

    # ─── Error notification ───────────────────────────────────────────

    async def _notify_error(self, task: str, error: str) -> None:
        logger.error("historical_sync.task_failed", task=task, error=error)
        try:
            from notifications import get_notifier
            await get_notifier().unhandled_exception(
                location=f"historical_sync.{task}",
                error=error,
            )
        except Exception:
            pass
