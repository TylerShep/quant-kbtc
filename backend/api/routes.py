"""
REST API routes — health, status, trade history, equity history.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Query
from pydantic import BaseModel

from database import get_pool


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

    return {
        "tick_count": coordinator._tick_count,
        "kalshi_ws": kalshi_info,
        "spot_ws": spot_info,
        "atr_regime": coordinator.atr_filter.current_regime,
        "candle_count": len(candle_agg.candles),
        "last_candle_close": last_candle.close if last_candle else None,
        "last_candle_time": last_candle.timestamp if last_candle else None,
        "has_position": coordinator.paper_trader.has_position,
        "trading_mode": coordinator.trading_mode,
        "can_trade": coordinator.circuit_breaker.can_trade(),
        "book_healthy": book_healthy,
        "dashboard_ws_clients": len(dm._listeners),
    }


@router.get("/status")
async def status():
    from main import coordinator

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

    return {
        "market_states": states,
        "atr": coordinator.atr_filter.get_state(),
        "risk": coordinator.circuit_breaker.get_state(),
        "paper": coordinator.active_trader.get_state(),
        "trading_mode": coordinator.trading_mode,
        "trading_paused": coordinator.trading_paused,
        "paper_bankroll": round(coordinator.paper_sizer.bankroll, 2),
        "live_bankroll": round(coordinator.live_sizer.bankroll, 2),
    }


@router.post("/trading-mode")
async def set_trading_mode(req: TradingModeRequest):
    """Switch between paper and live trading modes."""
    from main import coordinator

    if req.mode not in ("paper", "live"):
        return {"error": "Mode must be 'paper' or 'live'", "success": False}

    if req.mode == "live" and not req.confirm:
        return {
            "error": "Switching to live requires confirm=true. This will use real funds.",
            "success": False,
            "requires_confirmation": True,
        }

    if coordinator.active_trader.has_position:
        return {
            "error": "Cannot switch mode while a position is open. Close position first.",
            "success": False,
        }

    old_mode = coordinator.trading_mode
    if old_mode == req.mode:
        return {"success": True, "mode": req.mode, "message": f"Already in {req.mode} mode"}

    coordinator.trading_mode = req.mode

    return {
        "success": True,
        "mode": req.mode,
        "previous_mode": old_mode,
        "message": f"Switched from {old_mode} to {req.mode} trading",
    }


@router.post("/trading-pause")
async def set_trading_pause(req: TradingPauseRequest):
    """Pause or resume automated trading. Pausing stops new entries but allows exits."""
    from main import coordinator

    coordinator.trading_paused = req.paused
    import asyncio
    asyncio.create_task(coordinator._save_state())
    return {
        "success": True,
        "paused": coordinator.trading_paused,
        "message": "Trading paused" if req.paused else "Trading resumed",
    }


@router.get("/trades")
async def trades(
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=100),
    mode: str = Query(None),
):
    """Paginated trade history from the database (survives restarts)."""
    from main import coordinator
    pool = await get_pool()
    offset = (page - 1) * per_page
    active_mode = mode or coordinator.trading_mode

    async with pool.connection() as conn:
        row = await conn.execute(
            "SELECT COUNT(*) FROM trades WHERE trading_mode = %s", (active_mode,)
        )
        total = (await row.fetchone())[0]

        rows = await conn.execute(
            """SELECT timestamp, ticker, direction, contracts, entry_price,
                      exit_price, pnl, pnl_pct, fees, exit_reason, conviction,
                      regime_at_entry, candles_held, closed_at
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
        })

    return {
        "trades": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    }


@router.get("/errored-trades")
async def errored_trades(
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=100),
):
    """Paginated errored/quarantined trade history."""
    pool = await get_pool()
    offset = (page - 1) * per_page

    async with pool.connection() as conn:
        row = await conn.execute("SELECT COUNT(*) FROM errored_trades")
        total = (await row.fetchone())[0]

        rows = await conn.execute(
            """SELECT timestamp, ticker, direction, contracts, entry_price,
                      exit_price, pnl, pnl_pct, fees, exit_reason, conviction,
                      regime_at_entry, candles_held, closed_at, error_reason, flagged_at
               FROM errored_trades
               ORDER BY timestamp DESC
               LIMIT %s OFFSET %s""",
            (per_page, offset),
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
        })

    return {
        "trades": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    }


@router.get("/equity")
async def equity(mode: str = Query(None)):
    """Equity curve data from bankroll_history (survives restarts)."""
    from main import coordinator
    pool = await get_pool()
    active_mode = mode or coordinator.trading_mode

    async with pool.connection() as conn:
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

    return {"equity": items, "mode": active_mode}


@router.get("/stats")
async def stats(mode: str = Query(None)):
    """Cumulative stats from all trades in DB (survives restarts)."""
    from config import settings
    from main import coordinator

    pool = await get_pool()
    active_mode = mode or coordinator.trading_mode

    async with pool.connection() as conn:
        row = await conn.execute(
            """SELECT
                 COUNT(*) as total_trades,
                 COALESCE(SUM(pnl), 0) as total_pnl,
                 COUNT(*) FILTER (WHERE pnl >= 0) as wins,
                 COUNT(*) FILTER (WHERE pnl < 0) as losses,
                 COALESCE(MAX(pnl), 0) as best_trade,
                 COALESCE(MIN(pnl), 0) as worst_trade,
                 COALESCE(AVG(pnl), 0) as avg_pnl
               FROM trades
               WHERE trading_mode = %s""",
            (active_mode,),
        )
        r = await row.fetchone()

    initial = settings.bot.initial_bankroll
    total_pnl = float(r[1])

    return {
        "initial_bankroll": initial,
        "total_trades": r[0],
        "total_pnl": total_pnl,
        "equity": round(initial + total_pnl, 4),
        "wins": r[2],
        "losses": r[3],
        "win_rate": r[2] / r[0] if r[0] > 0 else 0,
        "best_trade": float(r[4]),
        "worst_trade": float(r[5]),
        "avg_pnl": float(r[6]),
        "mode": active_mode,
    }


@router.get("/attribution")
async def attribution(mode: str = Query(None)):
    """Performance attribution — decompose PnL by signal, regime, session, execution."""
    from main import coordinator
    pool = await get_pool()
    active_mode = mode or coordinator.trading_mode

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
    return {"attribution": run_attribution(trades), "mode": active_mode}


@router.get("/param-overrides")
async def get_param_overrides():
    """Show currently active parameter overrides from auto-tuner."""
    from main import coordinator
    return {
        "overrides": coordinator.param_overrides,
        "active": bool(coordinator.param_overrides),
    }


@router.delete("/param-overrides")
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
    pool = await get_pool()
    active_mode = mode or coordinator.trading_mode

    async with pool.connection() as conn:
        rows = await conn.execute(
            """SELECT DATE(timestamp) as day,
                      COUNT(*) as trades,
                      COALESCE(SUM(pnl), 0) as pnl,
                      COUNT(*) FILTER (WHERE pnl >= 0) as wins,
                      COUNT(*) FILTER (WHERE pnl < 0) as losses
               FROM trades
               WHERE trading_mode = %s
               GROUP BY DATE(timestamp)
               ORDER BY day ASC""",
            (active_mode,),
        )
        result = await rows.fetchall()

    return {
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


@router.get("/stats/by-regime")
async def stats_by_regime(mode: str = Query(None)):
    """Win rate and PnL by ATR regime."""
    from main import coordinator
    pool = await get_pool()
    active_mode = mode or coordinator.trading_mode

    async with pool.connection() as conn:
        rows = await conn.execute(
            """SELECT regime_at_entry,
                      COUNT(*) as trades,
                      COALESCE(SUM(pnl), 0) as pnl,
                      COUNT(*) FILTER (WHERE pnl >= 0) as wins
               FROM trades
               WHERE trading_mode = %s
               GROUP BY regime_at_entry
               ORDER BY trades DESC""",
            (active_mode,),
        )
        result = await rows.fetchall()

    return {
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
    """BTC price history from candles table + in-memory aggregator."""
    from main import coordinator

    candles = coordinator.candle_aggregator.recent(500)
    items = [
        {
            "time": int(c.timestamp),
            "open": round(c.open, 2),
            "high": round(c.high, 2),
            "low": round(c.low, 2),
            "close": round(c.close, 2),
            "volume": round(c.volume, 2),
        }
        for c in candles
    ]

    if not items:
        pool = await get_pool()
        async with pool.connection() as conn:
            rows = await conn.execute(
                """SELECT timestamp, open, high, low, close, volume
                   FROM candles
                   WHERE source IN ('live_spot', 'binance')
                   ORDER BY timestamp DESC
                   LIMIT 500"""
            )
            result = await rows.fetchall()
            items = [
                {
                    "time": int(r[0].timestamp()),
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "volume": float(r[5]),
                }
                for r in reversed(result)
            ]

    return {"candles": items}
