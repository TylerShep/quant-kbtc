"""
REST API routes — health, status, trade history, equity history.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from database import get_pool

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}


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
        "paper": coordinator.paper_trader.get_state(),
    }


@router.get("/trades")
async def trades(
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=100),
):
    """Paginated trade history from the database (survives restarts)."""
    pool = await get_pool()
    offset = (page - 1) * per_page

    async with pool.connection() as conn:
        row = await conn.execute("SELECT COUNT(*) FROM trades")
        total = (await row.fetchone())[0]

        rows = await conn.execute(
            """SELECT timestamp, ticker, direction, contracts, entry_price,
                      exit_price, pnl, pnl_pct, fees, exit_reason, conviction,
                      regime_at_entry, candles_held, closed_at
               FROM trades
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
async def equity():
    """Equity curve data from bankroll_history (survives restarts)."""
    pool = await get_pool()
    async with pool.connection() as conn:
        rows = await conn.execute(
            """SELECT timestamp, bankroll, peak_bankroll, drawdown_pct, daily_pnl, trade_count
               FROM bankroll_history
               ORDER BY timestamp ASC"""
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

    return {"equity": items}


@router.get("/stats")
async def stats():
    """Cumulative stats from all trades in DB (survives restarts)."""
    from config import settings

    pool = await get_pool()
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
               FROM trades"""
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
    }
