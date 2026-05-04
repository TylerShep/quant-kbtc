"""
REST API routes — health, status, trade history, equity history.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from api.auth import require_api_token
from database import get_pool
from database.connection import pool_stats as db_pool_stats


class _TTLCache:
    """Simple in-memory TTL cache keyed by (endpoint, mode)."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, ttl: float) -> Any | None:
        entry = self._data.get(key)
        if entry and (time.monotonic() - entry[0]) < ttl:
            return entry[1]
        return None

    def set(self, key: str, value: Any) -> None:
        self._data[key] = (time.monotonic(), value)

    def invalidate(self, prefix: str = "") -> None:
        if not prefix:
            self._data.clear()
        else:
            self._data = {k: v for k, v in self._data.items() if not k.startswith(prefix)}


_cache = _TTLCache()

_EQUITY_TTL = 10.0
_STATS_TTL = 10.0
_TRADES_TTL = 5.0
_DAILY_TTL = 15.0
_REGIME_TTL = 15.0
_ATTR_TTL = 30.0

MECHANICAL_EXIT_REASONS = ("ORPHAN_SETTLED", "TICKER_ROLLED", "RETRY")
_MECHANICAL_FILTER = "AND exit_reason NOT IN ('ORPHAN_SETTLED', 'TICKER_ROLLED', 'RETRY')"


class TradingModeRequest(BaseModel):
    mode: str
    confirm: bool = False


class TradingPauseRequest(BaseModel):
    paused: bool

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/diagnostics")
async def diagnostics():
    """System health diagnostics for debugging live data issues."""
    from main import coordinator
    from monitoring.live_health import fetch_edge_profile_health

    dm = coordinator.data_manager
    now = time.time()

    kalshi_ws = dm._kalshi_ws
    spot_ws = dm._spot_ws

    kalshi_info = {
        "connected": kalshi_ws.connected if kalshi_ws else False,
        "last_message_age_sec": round(now - kalshi_ws.last_message_time, 1) if kalshi_ws and kalshi_ws.last_message_time else None,
        "message_count": kalshi_ws.message_count if kalshi_ws else 0,
        "connect_attempts": kalshi_ws.connect_attempts if kalshi_ws else 0,
        "active_tickers": dict(kalshi_ws.active_tickers) if kalshi_ws else {},
    }

    spot_info = {
        "connected": spot_ws.connected if spot_ws else False,
        "last_message_age_sec": round(now - spot_ws.last_message_time, 1) if spot_ws and spot_ws.last_message_time else None,
        "message_count": spot_ws.message_count if spot_ws else 0,
        "connect_attempts": spot_ws.connect_attempts if spot_ws else 0,
        "latest_prices": dict(spot_ws._latest) if spot_ws else {},
    }

    candle_agg = coordinator.candle_aggregator
    last_candle = candle_agg.candles[-1] if candle_agg.candles else None

    state = dm.states.get("BTC")
    book_healthy = coordinator._is_book_healthy(state) if state else False

    edge_health: dict[str, Any] = {}
    try:
        pool = await get_pool()
        edge_health = await fetch_edge_profile_health(pool)
    except Exception:
        edge_health = {"error": "unavailable"}

    return {
        "tick_count": coordinator._tick_count,
        "kalshi_ws": kalshi_info,
        "spot_ws": spot_info,
        "atr_regime": coordinator.atr_filter.current_regime,
        "candle_count": len(candle_agg.candles),
        "last_candle_close": last_candle.close if last_candle else None,
        "last_candle_time": last_candle.timestamp if last_candle else None,
        "has_paper_position": coordinator.paper_trader.has_position,
        "has_live_position": coordinator.live_trader.has_position,
        "trading_mode": coordinator.trading_mode,
        "can_trade": coordinator.circuit_breaker.can_trade(),
        "book_healthy": book_healthy,
        "dashboard_ws_clients": len(dm._listeners),
        "near_expiry_skips": dict(coordinator._near_expiry_skip_count),
        "edge_profile_health": edge_health,
        "db_pool": db_pool_stats(),
        "bg_persist": {
            "queued": len(coordinator._bg_persist_tasks),
            "max": coordinator._bg_persist_max,
            "dropped_total": coordinator._bg_persist_dropped,
        },
    }


@router.get("/status")
async def status():
    from main import coordinator
    from monitoring.live_health import fetch_edge_profile_health

    states = {}
    for symbol, state in coordinator.data_manager.states.items():
        states[symbol] = {
            "spot_price": state.spot_price,
            "kalshi_ticker": state.kalshi_ticker,
            "best_bid": state.order_book.best_yes_bid,
            "best_ask": state.order_book.best_yes_ask,
            "mid": state.order_book.mid,
            "spread": state.order_book.spread,
            "obi": round(state.order_book.obi(), 4),
            "time_remaining_sec": state.time_remaining_sec,
            "volume": state.volume,
        }

    orphans = [
        {
            "ticker": o.ticker,
            "direction": o.direction,
            "contracts": o.contracts,
            "avg_entry_price": o.avg_entry_price,
            "detected_at": (
                o.detected_at if isinstance(o.detected_at, str)
                else o.detected_at.isoformat() if o.detected_at is not None
                else None
            ),
        }
        for o in coordinator.live_trader.orphaned_positions
    ]

    wallet_balance = None
    if coordinator.live_enabled:
        try:
            balance_data = await coordinator.live_trader.client.get_balance()
            wallet_balance = round(float(balance_data.get("balance", 0)) / 100, 2)
        except Exception:
            pass

    fill_stream_status = None
    if coordinator.fill_stream is not None:
        fs = coordinator.fill_stream
        fill_stream_status = {
            "connected": bool(fs.connected),
            "message_count": fs.message_count,
            "last_message_age_sec": (
                round(time.time() - fs.last_message_time, 2)
                if fs.last_message_time is not None else None
            ),
            "connect_attempts": fs.connect_attempts,
        }

    edge_health: dict[str, Any] = {}
    try:
        pool = await get_pool()
        edge_health = await fetch_edge_profile_health(pool)
    except Exception:
        edge_health = {"error": "unavailable"}

    return {
        "market_states": states,
        "atr": coordinator.atr_filter.get_state(),
        "risk": coordinator.circuit_breaker.get_state(),
        "paper": coordinator.paper_trader.get_state(),
        "live": coordinator.live_trader.get_state(),
        "trading_mode": coordinator.trading_mode,
        "trading_paused": coordinator.trading_paused,
        "paper_bankroll": round(coordinator.paper_sizer.bankroll, 2),
        "live_bankroll": round(coordinator.live_sizer.bankroll, 2),
        "wallet_balance": wallet_balance,
        "orphaned_positions": orphans,
        "paper_decision": coordinator._serialize_decision("paper"),
        "live_decision": coordinator._serialize_decision("live") if coordinator.live_enabled else None,
        "paper_risk": coordinator.paper_breaker.get_state(),
        "live_risk": coordinator.live_breaker.get_state(),
        "fill_stream": fill_stream_status,
        "edge_profile_health": edge_health,
        "db_pool": db_pool_stats(),
    }


@router.post("/trading-mode", dependencies=[Depends(require_api_token)])
async def set_trading_mode(req: TradingModeRequest):
    """Switch between paper and live trading modes.

    Switching to live used to block on a synchronous Kalshi /portfolio/balance
    fetch; that endpoint has a long tail (we've measured 15s+ in the wild),
    so the toggle felt sluggish or hung. We now flip the mode immediately
    and run the wallet refresh in the background, broadcasting the fresh
    bankroll over WebSocket once it lands. The cached ``live_sizer.bankroll``
    (last persisted on the previous sync) is correct enough to size a trade
    against; the periodic reconciler will overwrite it within 30s if the
    background fetch hiccups.
    """
    from main import coordinator
    import asyncio

    if req.mode not in ("paper", "live"):
        return {"error": "Mode must be 'paper' or 'live'", "success": False}

    if req.mode == "live" and not req.confirm:
        return {
            "error": "Switching to live requires confirm=true. This will use real funds.",
            "success": False,
            "requires_confirmation": True,
        }

    if coordinator.trading_paused == "settling":
        return {
            "error": "Settling open live position, please wait.",
            "success": False,
        }

    if req.mode == "paper" and coordinator.live_trader.has_position:
        return {
            "error": "Cannot disable live while a live position is open. Pause trading first to settle.",
            "success": False,
        }

    old_mode = coordinator.trading_mode
    if old_mode == req.mode:
        return {"success": True, "mode": req.mode, "message": f"Already in {req.mode} mode"}

    coordinator.trading_mode = req.mode
    if req.mode == "paper":
        coordinator.trading_paused = "off"
    asyncio.create_task(coordinator._save_state())

    if req.mode == "live":
        asyncio.create_task(_refresh_live_bankroll_in_background())

    resp = {
        "success": True,
        "mode": req.mode,
        "previous_mode": old_mode,
        "message": f"Switched from {old_mode} to {req.mode} trading",
    }
    if req.mode == "live":
        resp["live_bankroll"] = round(coordinator.live_sizer.bankroll, 2)
        resp["bankroll_refresh"] = "pending"
    return resp


async def _refresh_live_bankroll_in_background() -> None:
    """Fetch fresh Kalshi balance after a live-mode toggle and notify
    dashboards. Failures are non-fatal — the cached bankroll keeps the bot
    operational and the periodic reconciler will retry within 30s."""
    from main import coordinator
    from api.ws import ws_manager
    import structlog

    log = structlog.get_logger(__name__)
    try:
        wallet = await coordinator.sync_live_bankroll()
        await ws_manager.broadcast({
            "type": "live_bankroll_refreshed",
            "wallet": round(wallet, 2),
            "live_bankroll": round(coordinator.live_sizer.bankroll, 2),
        })
    except Exception as e:
        log.warning("api.toggle_bankroll_refresh_failed", error=str(e))
        await ws_manager.broadcast({
            "type": "live_bankroll_refresh_failed",
            "error": str(e),
            "live_bankroll": round(coordinator.live_sizer.bankroll, 2),
        })


@router.post("/reset-drawdown", dependencies=[Depends(require_api_token)])
async def reset_drawdown(mode: str = None):
    """Reset drawdown peak to current bankroll for the given mode (paper/live)."""
    from main import coordinator
    import asyncio

    target_mode = mode or coordinator.trading_mode

    if target_mode == "live":
        try:
            await coordinator.sync_live_bankroll()
        except Exception as e:
            return {"success": False, "error": f"Failed to fetch balance: {e}"}
        sizer = coordinator.live_sizer
    else:
        sizer = coordinator.paper_sizer

    old_peak = sizer.peak_bankroll
    sizer.peak_bankroll = sizer.bankroll
    sizer.daily_start_bankroll = sizer.bankroll
    sizer.weekly_start_bankroll = sizer.bankroll

    asyncio.create_task(coordinator._save_state())

    return {
        "success": True,
        "mode": target_mode,
        "bankroll": round(sizer.bankroll, 2),
        "old_peak": round(old_peak, 2),
        "new_peak": round(sizer.peak_bankroll, 2),
        "drawdown_pct": round(sizer.current_drawdown * 100, 2),
    }


@router.post("/trade-limit", dependencies=[Depends(require_api_token)])
async def set_trade_limit(req: dict = {}):
    """Set or reset the live trade limit.

    Body: {"limit": 1}     → allow 1 more trade then stop
          {"limit": null}   → remove limit (unlimited)
          {"reset": true}   → reset counter to 0 (allow limit more trades)
    """
    from main import coordinator

    pm = coordinator.live_trader.position_manager
    new_limit = req.get("limit", pm.live_trade_limit)
    reset = req.get("reset", False)

    if reset:
        pm.reset_trade_counter()

    if "limit" in req:
        pm.live_trade_limit = new_limit

    return {
        "success": True,
        "live_trade_limit": pm.live_trade_limit,
        "completed_live_trades": pm._completed_live_trades,
        "can_enter": pm.can_enter,
    }


@router.post("/trading-pause", dependencies=[Depends(require_api_token)])
async def set_trading_pause(req: TradingPauseRequest):
    """Pause or resume automated trading. Pausing stops new entries but allows exits."""
    from main import coordinator

    if req.paused:
        if coordinator.live_trader.has_position:
            coordinator.trading_paused = "settling"
            status = "settling"
            message = "Waiting for open live position to exit..."
        else:
            coordinator.trading_paused = "paused"
            status = "paused"
            message = "Trading paused"
    else:
        coordinator.trading_paused = "off"
        status = "off"
        message = "Trading resumed"

        pm = coordinator.live_trader.position_manager
        if pm.live_trade_limit is not None:
            pm.reset_trade_counter()
            message = f"Trading resumed (trade counter reset, limit={pm.live_trade_limit})"

    import asyncio
    asyncio.create_task(coordinator._save_state())
    return {
        "success": True,
        "trading_paused": status,
        "message": message,
    }


@router.get("/deploy-check")
async def deploy_check():
    """Pre-deploy safety check: returns whether it's safe to restart the bot."""
    from main import coordinator

    live_position = coordinator.live_trader.has_position
    orphans = len(coordinator.live_trader.orphaned_positions)
    pm_busy = coordinator.live_trader.position_manager.is_busy
    pm_state = coordinator.live_trader.position_manager.state.value
    is_live = coordinator.trading_mode == "live"

    resting_count = 0
    try:
        orders_data = await coordinator.live_trader.client.get_orders(status="resting")
        resting_orders = orders_data.get("orders", [])
        resting_count = sum(
            1 for o in resting_orders
            if any(o.get("ticker", "").startswith(p) for p in ("KXBTC", "KXETH"))
        )
    except Exception:
        pass

    blockers = []
    if live_position:
        pos = coordinator.live_trader.position
        if pos is not None:
            blockers.append(f"Open live position: {pos.ticker} ({pos.direction}, {pos.contracts} contracts)")
        else:
            blockers.append("Open live position (details unavailable — state changed during check)")
    if resting_count > 0:
        blockers.append(f"{resting_count} resting order(s) on Kalshi")
    if pm_busy:
        blockers.append(f"PositionManager busy (state: {pm_state})")

    safe = len(blockers) == 0

    return {
        "safe_to_deploy": safe,
        "trading_mode": coordinator.trading_mode,
        "blockers": blockers,
        "orphans": orphans,
        "message": "Safe to deploy" if safe else "BLOCKED: " + "; ".join(blockers),
    }


@router.post("/emergency-stop", dependencies=[Depends(require_api_token)])
async def emergency_stop():
    """Kill switch: force-close all positions and halt trading immediately.

    Uses PositionManager's lock to prevent concurrent orders with the
    tick loop. Retries and orphan conversion handled by PositionManager.
    """
    from main import coordinator
    import asyncio

    coordinator.trading_paused = "paused"
    results = {"paused": True, "actions": []}

    trader = coordinator.live_trader
    if trader.has_position:
        ticker = trader.position.ticker
        trade = await trader.emergency_close()
        if trade:
            coordinator._on_trade_exit(trade, "BTC", "live")
            results["actions"].append(f"Closed {ticker}: pnl={trade.pnl}")
        else:
            results["actions"].append(
                f"Exit failed for {ticker}, converted to orphan"
            )
            coordinator._unregister_position_ticker(ticker)

    paper = coordinator.paper_trader
    if paper.has_position:
        trade = paper.exit(paper.position.entry_price, "EMERGENCY_STOP")
        if trade:
            coordinator._on_trade_exit(trade, "BTC", "paper")
            results["actions"].append("Paper position closed")

    asyncio.create_task(coordinator._save_state())

    from notifications import get_notifier
    asyncio.create_task(get_notifier().unhandled_exception(
        location="api.emergency_stop",
        error="Emergency stop triggered via API",
    ))

    results["success"] = True
    results["message"] = "Emergency stop executed. Trading halted."
    return results


@router.post("/close-all-exchange-positions", dependencies=[Depends(require_api_token)])
async def close_all_exchange_positions():
    """Query Kalshi for ALL open positions and close them directly on the exchange.

    Uses PositionManager's lock to prevent concurrent orders with the tick loop.
    Unlike emergency-stop, this reads actual exchange state and closes everything.
    """
    from main import coordinator
    import asyncio

    coordinator.trading_paused = "paused"
    trader = coordinator.live_trader
    results = {"paused": True, "actions": [], "positions_found": 0}

    old_ticker = trader.position.ticker if trader.has_position else None

    close_results = await trader.close_all_exchange_positions()
    results["positions_found"] = len(close_results)

    for cr in close_results:
        if cr.get("status") == "closed":
            results["actions"].append(
                f"Closed {cr['ticker']}: {cr['direction']} x{cr['contracts']}, "
                f"order={cr.get('order_id')}, filled={cr.get('filled')}"
            )
        else:
            results["actions"].append(f"Failed to close {cr['ticker']}: {cr.get('error')}")

    if old_ticker:
        coordinator._unregister_position_ticker(old_ticker)

    try:
        await coordinator.sync_live_bankroll()
        results["wallet_synced"] = True
        results["wallet_balance"] = round(coordinator.live_sizer.bankroll, 2)
    except Exception as e:
        results["wallet_synced"] = False
        results["wallet_sync_error"] = str(e)

    asyncio.create_task(coordinator._save_state())

    from notifications import get_notifier
    asyncio.create_task(get_notifier().unhandled_exception(
        location="api.close_all_exchange_positions",
        error=f"Force-closed {len(close_results)} exchange positions",
    ))

    results["success"] = True
    results["message"] = f"Closed {len(close_results)} positions directly on Kalshi."
    return results


@router.get("/trades")
async def trades(
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=100),
    mode: str = Query(None),
):
    """Paginated trade history from the database (survives restarts)."""
    from main import coordinator
    active_mode = mode or coordinator.trading_mode
    offset = (page - 1) * per_page

    cache_key = f"trades:{active_mode}:{page}:{per_page}"
    cached = _cache.get(cache_key, _TRADES_TTL)
    if cached is not None:
        return cached

    pool = await get_pool()
    async with pool.connection() as conn:
        row = await conn.execute(
            "SELECT COUNT(*) FROM trades WHERE trading_mode = %s", (active_mode,)
        )
        total = (await row.fetchone())[0]

        rows = await conn.execute(
            """SELECT timestamp, ticker, direction, contracts, entry_price,
                      exit_price, pnl, pnl_pct, fees, exit_reason, conviction,
                      regime_at_entry, candles_held, closed_at, signal_driver
               FROM trades
               WHERE trading_mode = %s
               ORDER BY timestamp DESC
               LIMIT %s OFFSET %s""",
            (active_mode, per_page, offset),
        )
        results = await rows.fetchall()

    items = []
    for r in results:
        items.append({
            "timestamp": r[0].isoformat() if r[0] else None,
            "ticker": r[1],
            "direction": r[2],
            "contracts": r[3],
            "entry_price": float(r[4]) if r[4] is not None else None,
            "exit_price": float(r[5]) if r[5] is not None else None,
            "pnl": float(r[6]) if r[6] is not None else 0,
            "pnl_pct": float(r[7]) if r[7] is not None else 0,
            "fees": float(r[8]) if r[8] is not None else 0,
            "exit_reason": r[9],
            "conviction": r[10],
            "regime_at_entry": r[11],
            "candles_held": r[12],
            "closed_at": r[13].isoformat() if r[13] else None,
            "signal_driver": r[14] or "-",
        })

    result = {
        "trades": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    }
    _cache.set(cache_key, result)
    return result


@router.get("/errored-trades")
async def errored_trades(
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=100),
    mode: str = Query(None),
):
    """Paginated errored/quarantined trade history."""
    from main import coordinator
    pool = await get_pool()
    offset = (page - 1) * per_page
    active_mode = mode or coordinator.trading_mode

    async with pool.connection() as conn:
        row = await conn.execute(
            "SELECT COUNT(*) FROM errored_trades WHERE trading_mode = %s",
            (active_mode,),
        )
        total = (await row.fetchone())[0]

        rows = await conn.execute(
            """SELECT timestamp, ticker, direction, contracts, entry_price,
                      exit_price, pnl, pnl_pct, fees, exit_reason, conviction,
                      regime_at_entry, candles_held, closed_at, error_reason, flagged_at,
                      signal_driver
               FROM errored_trades
               WHERE trading_mode = %s
               ORDER BY timestamp DESC
               LIMIT %s OFFSET %s""",
            (active_mode, per_page, offset),
        )
        results = await rows.fetchall()

    items = []
    for r in results:
        items.append({
            "timestamp": r[0].isoformat() if r[0] else None,
            "ticker": r[1],
            "direction": r[2],
            "contracts": r[3],
            "entry_price": float(r[4]) if r[4] is not None else None,
            "exit_price": float(r[5]) if r[5] is not None else None,
            "pnl": float(r[6]) if r[6] is not None else 0,
            "pnl_pct": float(r[7]) if r[7] is not None else 0,
            "fees": float(r[8]) if r[8] is not None else 0,
            "exit_reason": r[9],
            "conviction": r[10],
            "regime_at_entry": r[11],
            "candles_held": r[12],
            "closed_at": r[13].isoformat() if r[13] else None,
            "error_reason": r[14],
            "flagged_at": r[15].isoformat() if r[15] else None,
            "signal_driver": r[16] or "-",
        })

    return {
        "trades": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    }


@router.get("/equity")
async def equity(mode: str = Query(None), days: int = Query(30)):
    """Equity curve data from bankroll_history (survives restarts).

    2026-05-04 (BUG-032 follow-up #2): bounded by ``days`` (default 30).
    Without it, the unbounded ORDER BY scan over 1M+ rows took 27+
    seconds per call, holding a DB connection and starving every other
    write task — the cascade pinned bg-persist queue memory and
    eventually SIGKILLed the container. Combined with the new
    ``(trading_mode, timestamp DESC)`` index, the bounded query now
    returns in <50ms on the same dataset. ``days=0`` keeps the legacy
    full-history behavior for one-off ad-hoc requests.
    """
    from datetime import datetime, timezone, timedelta
    from main import coordinator
    active_mode = mode or coordinator.trading_mode

    cache_key = f"equity:{active_mode}:{days}"
    cached = _cache.get(cache_key, _EQUITY_TTL)
    if cached is not None:
        return cached

    pool = await get_pool()
    async with pool.connection() as conn:
        if days and days > 0:
            since = datetime.now(timezone.utc) - timedelta(days=days)
            rows = await conn.execute(
                """SELECT timestamp, bankroll, peak_bankroll, drawdown_pct, daily_pnl, trade_count
                   FROM bankroll_history
                   WHERE trading_mode = %s AND timestamp >= %s
                   ORDER BY timestamp ASC""",
                (active_mode, since),
            )
        else:
            rows = await conn.execute(
                """SELECT timestamp, bankroll, peak_bankroll, drawdown_pct, daily_pnl, trade_count
                   FROM bankroll_history
                   WHERE trading_mode = %s
                   ORDER BY timestamp ASC""",
                (active_mode,),
            )
        results = await rows.fetchall()

    items = []
    for r in results:
        items.append({
            "time": int(r[0].timestamp()) if r[0] else 0,
            "bankroll": float(r[1]) if r[1] is not None else 0,
            "peak_bankroll": float(r[2]) if r[2] is not None else 0,
            "drawdown_pct": float(r[3]) if r[3] is not None else 0,
            "daily_pnl": float(r[4]) if r[4] is not None else 0,
            "trade_count": r[5] or 0,
        })

    result = {"equity": items, "mode": active_mode}
    _cache.set(cache_key, result)
    return result


@router.get("/stats")
async def stats(mode: str = Query(None)):
    """Cumulative stats from all trades in DB (survives restarts)."""
    from config import settings
    from main import coordinator

    active_mode = mode or coordinator.trading_mode

    cache_key = f"stats:{active_mode}"
    cached = _cache.get(cache_key, _STATS_TTL)
    if cached is not None:
        return cached

    pool = await get_pool()
    async with pool.connection() as conn:
        row = await conn.execute(
            f"""SELECT
                 COUNT(*) as total_trades,
                 COALESCE(SUM(pnl), 0) as total_pnl,
                 COUNT(*) FILTER (WHERE pnl >= 0) as wins,
                 COUNT(*) FILTER (WHERE pnl < 0) as losses,
                 COALESCE(MAX(pnl), 0) as best_trade,
                 COALESCE(MIN(pnl), 0) as worst_trade,
                 COALESCE(AVG(pnl), 0) as avg_pnl
               FROM trades
               WHERE trading_mode = %s {_MECHANICAL_FILTER}""",
            (active_mode,),
        )
        r = await row.fetchone()

    total_pnl = float(r[1])

    wallet_equity = None
    if active_mode == "live":
        equity = round(coordinator.live_sizer.bankroll, 4)
        initial = equity - total_pnl
        try:
            balance_data = await coordinator.live_trader.client.get_balance()
            wallet_equity = round(float(balance_data.get("balance", 0)) / 100, 4)
        except Exception:
            pass
    else:
        initial = settings.bot.initial_bankroll
        equity = round(initial + total_pnl, 4)

    result = {
        "initial_bankroll": round(initial, 4),
        "total_trades": r[0],
        "total_pnl": total_pnl,
        "equity": equity,
        "wallet_equity": wallet_equity,
        "wins": r[2],
        "losses": r[3],
        "win_rate": r[2] / r[0] if r[0] > 0 else 0,
        "best_trade": float(r[4]),
        "worst_trade": float(r[5]),
        "avg_pnl": float(r[6]),
        "mode": active_mode,
    }
    _cache.set(cache_key, result)
    return result


@router.get("/attribution")
async def attribution(mode: str = Query(None)):
    """Performance attribution — decompose PnL by signal, regime, session, execution."""
    from main import coordinator
    active_mode = mode or coordinator.trading_mode

    cache_key = f"attr:{active_mode}"
    cached = _cache.get(cache_key, _ATTR_TTL)
    if cached is not None:
        return cached

    pool = await get_pool()
    async with pool.connection() as conn:
        rows = await conn.execute(
            """SELECT timestamp, ticker, direction, contracts, entry_price,
                      exit_price, pnl, pnl_pct, fees, exit_reason, conviction,
                      regime_at_entry, candles_held, closed_at
               FROM trades
               WHERE trading_mode = %s
               ORDER BY timestamp ASC""",
            (active_mode,),
        )
        results = await rows.fetchall()

    trades = []
    for r in results:
        trades.append({
            "timestamp": r[0].timestamp() if r[0] else 0,
            "ticker": r[1],
            "direction": r[2],
            "contracts": r[3],
            "entry_price": float(r[4]) if r[4] is not None else 0,
            "exit_price": float(r[5]) if r[5] is not None else 0,
            "pnl": float(r[6]) if r[6] is not None else 0,
            "pnl_pct": float(r[7]) if r[7] is not None else 0,
            "fees": float(r[8]) if r[8] is not None else 0,
            "exit_reason": r[9],
            "conviction": r[10],
            "regime_at_entry": r[11],
            "candles_held": r[12],
            "exit_timestamp": r[13].timestamp() if r[13] else 0,
        })

    from backtesting.attribution import run_attribution
    result = {"attribution": run_attribution(trades), "mode": active_mode}
    _cache.set(cache_key, result)
    return result


@router.get("/param-overrides")
async def get_param_overrides():
    """Show currently active parameter overrides from auto-tuner."""
    from main import coordinator
    return {
        "overrides": coordinator.param_overrides,
        "active": bool(coordinator.param_overrides),
    }


@router.delete("/param-overrides", dependencies=[Depends(require_api_token)])
async def clear_param_overrides():
    """Clear parameter overrides, reverting to defaults."""
    from main import coordinator
    pool = await get_pool()
    from backtesting.auto_tuner import clear_applied_params
    await clear_applied_params(pool)
    coordinator.param_overrides = {}
    return {"success": True, "message": "Parameter overrides cleared, using defaults"}


@router.get("/backtest/latest")
async def backtest_latest():
    """Return latest backtest results from file if available."""
    import json as _json
    from pathlib import Path
    latest = Path("backtest_reports/latest.json")
    if not latest.exists():
        return {"available": False}
    try:
        data = _json.loads(latest.read_text())
        results = data.get("results", {})
        report_html = data.get("report_file")
        if not report_html:
            html_files = sorted(Path("backtest_reports").glob("*.html"))
            report_html = html_files[-1].name if html_files else None
        return {
            "available": True,
            "timestamp": data.get("timestamp"),
            "results": results,
            "trade_count": results.get("total_trades", 0),
            "config": data.get("config", {}),
            "report_file": report_html,
        }
    except Exception:
        return {"available": False}


@router.get("/backtest/report/{filename}")
async def backtest_report(filename: str):
    """Serve a generated HTML backtest report."""
    from pathlib import Path
    from fastapi.responses import FileResponse
    from fastapi import HTTPException
    path = Path("backtest_reports") / filename
    if not path.exists() or path.suffix != ".html":
        raise HTTPException(status_code=404, detail="Report not found")
    return FileResponse(str(path), media_type="text/html")


@router.get("/backtest/tuning")
async def backtest_tuning():
    """Return latest tuning recommendation."""
    import json as _json
    from pathlib import Path
    latest = Path("backtest_reports/tuning_latest.json")
    if not latest.exists():
        return {"available": False}
    try:
        return {"available": True, **_json.loads(latest.read_text())}
    except Exception:
        return {"available": False}


@router.get("/stats/daily")
async def stats_daily(mode: str = Query(None)):
    """Per-day PnL breakdown from trades table."""
    from main import coordinator
    active_mode = mode or coordinator.trading_mode

    cache_key = f"daily:{active_mode}"
    cached = _cache.get(cache_key, _DAILY_TTL)
    if cached is not None:
        return cached

    pool = await get_pool()
    async with pool.connection() as conn:
        rows = await conn.execute(
            f"""SELECT DATE(timestamp) as day,
                      COUNT(*) as trades,
                      COALESCE(SUM(pnl), 0) as pnl,
                      COUNT(*) FILTER (WHERE pnl >= 0) as wins,
                      COUNT(*) FILTER (WHERE pnl < 0) as losses
               FROM trades
               WHERE trading_mode = %s {_MECHANICAL_FILTER}
               GROUP BY DATE(timestamp)
               ORDER BY day ASC""",
            (active_mode,),
        )
        result = await rows.fetchall()

    result = {
        "daily": [
            {
                "date": r[0].isoformat() if r[0] else None,
                "trades": r[1],
                "pnl": float(r[2]),
                "wins": r[3],
                "losses": r[4],
                "win_rate": r[3] / r[1] if r[1] > 0 else 0,
            }
            for r in result
        ]
    }
    _cache.set(cache_key, result)
    return result


@router.get("/stats/by-regime")
async def stats_by_regime(mode: str = Query(None)):
    """Win rate and PnL by ATR regime."""
    from main import coordinator
    active_mode = mode or coordinator.trading_mode

    cache_key = f"regime:{active_mode}"
    cached = _cache.get(cache_key, _REGIME_TTL)
    if cached is not None:
        return cached

    pool = await get_pool()
    async with pool.connection() as conn:
        rows = await conn.execute(
            f"""SELECT regime_at_entry,
                      COUNT(*) as trades,
                      COALESCE(SUM(pnl), 0) as pnl,
                      COUNT(*) FILTER (WHERE pnl >= 0) as wins
               FROM trades
               WHERE trading_mode = %s {_MECHANICAL_FILTER}
               GROUP BY regime_at_entry
               ORDER BY trades DESC""",
            (active_mode,),
        )
        result = await rows.fetchall()

    result = {
        "regimes": [
            {
                "regime": r[0],
                "trades": r[1],
                "pnl": float(r[2]),
                "wins": r[3],
                "win_rate": r[3] / r[1] if r[1] > 0 else 0,
            }
            for r in result
        ]
    }
    _cache.set(cache_key, result)
    return result


@router.get("/stats/signal-accuracy")
async def stats_signal_accuracy():
    """OBI/ROC signal accuracy from signal_log vs trade outcomes."""
    pool = await get_pool()
    async with pool.connection() as conn:
        rows = await conn.execute(
            """SELECT obi_direction, roc_direction, decision, conviction, skip_reason,
                      COUNT(*) as count
               FROM signal_log
               GROUP BY obi_direction, roc_direction, decision, conviction, skip_reason
               ORDER BY count DESC
               LIMIT 50"""
        )
        result = await rows.fetchall()

    return {
        "signals": [
            {
                "obi_direction": r[0],
                "roc_direction": r[1],
                "decision": r[2],
                "conviction": r[3],
                "skip_reason": r[4],
                "count": r[5],
            }
            for r in result
        ]
    }


@router.get("/btc-price")
async def btc_price():
    """BTC price history: always loads DB history then merges live in-memory candles."""
    from main import coordinator

    items: list[dict] = []
    seen_times: set[int] = set()

    pool = await get_pool()
    if pool:
        try:
            async with pool.connection() as conn:
                rows = await conn.execute(
                    """SELECT timestamp, open, high, low, close, volume
                       FROM candles
                       WHERE source IN ('live_spot', 'binance')
                       ORDER BY timestamp DESC
                       LIMIT 500"""
                )
                result = await rows.fetchall()
                for r in reversed(result):
                    t = int(r[0].timestamp())
                    if t not in seen_times:
                        seen_times.add(t)
                        items.append({
                            "time": t,
                            "open": float(r[1]),
                            "high": float(r[2]),
                            "low": float(r[3]),
                            "close": float(r[4]),
                            "volume": float(r[5]),
                        })
        except Exception:
            pass

    for c in coordinator.candle_aggregator.recent(500):
        t = int(c.timestamp)
        if t not in seen_times:
            seen_times.add(t)
            items.append({
                "time": t,
                "open": round(c.open, 2),
                "high": round(c.high, 2),
                "low": round(c.low, 2),
                "close": round(c.close, 2),
                "volume": round(c.volume, 2),
            })

    items.sort(key=lambda x: x["time"])
    return {"candles": items}


@router.get("/historical-sync/status")
async def historical_sync_status():
    """Row counts, newest timestamps, sync lag, and TFI cache summary."""
    from main import coordinator
    from config import settings
    from datetime import datetime, timezone

    cfg = settings.historical_sync
    pool = await get_pool()

    async def _table_stats(table: str, ts_col: str) -> dict:
        async with pool.connection() as conn:
            row = await conn.execute(
                f"SELECT COUNT(*), MAX({ts_col}) FROM {table}"
            )
            count, newest = await row.fetchone()
        lag = None
        newest_iso = None
        if newest is not None:
            if newest.tzinfo is None:
                newest = newest.replace(tzinfo=timezone.utc)
            newest_iso = newest.isoformat()
            lag = int((datetime.now(timezone.utc) - newest).total_seconds())
        return {
            "table": table,
            "row_count": count or 0,
            "newest": newest_iso,
            "sync_lag_sec": lag,
        }

    settlements = await _table_stats("kalshi_markets", "close_time")
    settlements["sync_interval_sec"] = cfg.settlement_interval_sec

    trades = await _table_stats("kalshi_trades", "created_time")
    trades["sync_interval_sec"] = cfg.trades_interval_sec

    ob = await _table_stats("ob_snapshots", "timestamp")
    ob["sync_interval_sec"] = cfg.predexon_interval_sec

    tfi_summary = {"tickers_cached": 0, "sample_ticker": None, "sample_tfi": None}
    hs = getattr(coordinator, "historical_sync", None)
    if hs and hasattr(hs, "_tfi_cache") and hs._tfi_cache:
        cache = hs._tfi_cache
        tfi_summary["tickers_cached"] = len(cache)
        sample_ticker = next(iter(cache))
        tfi_summary["sample_ticker"] = sample_ticker
        tfi_val = hs.get_tfi(sample_ticker)
        tfi_summary["sample_tfi"] = round(tfi_val, 4) if tfi_val is not None else None

    return {
        "enabled": cfg.enabled,
        "pipelines": {
            "settlements": settlements,
            "trades": trades,
            "ob_snapshots": ob,
        },
        "tfi_cache": tfi_summary,
    }
