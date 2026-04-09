"""
REST API routes — health, status, config, trade history.
"""
from __future__ import annotations

from fastapi import APIRouter

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
