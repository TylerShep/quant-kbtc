"""Deterministic Kalshi API response emulator for orphan incident replays.

Provides a FakeKalshiClient that returns scripted responses based on a
timeline of events, allowing exact reproduction of production failure
sequences without touching the real exchange.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ExchangePosition:
    """A position the fake exchange will report via get_positions."""
    ticker: str
    position_fp: float
    total_traded_dollars: float = 0.0


@dataclass
class MarketStatus:
    """Market status the fake exchange will report via get_market."""
    ticker: str
    status: str = "open"
    result: str = ""


@dataclass
class TimelineEvent:
    """A single point-in-time snapshot of exchange state.

    The FakeKalshiClient advances through events sequentially.
    Each call to get_positions/get_market consumes the current event
    and advances the pointer (round-robin within the current step,
    then advance on explicit step()).
    """
    positions: List[ExchangePosition] = field(default_factory=list)
    markets: Dict[str, MarketStatus] = field(default_factory=dict)
    verify_fails: bool = False
    order_result: Optional[dict] = None


class FakeKalshiClient:
    """Mock Kalshi client driven by a scripted timeline.

    Usage:
        timeline = [
            TimelineEvent(positions=[...], markets={...}),
            TimelineEvent(positions=[], markets={...}),
        ]
        client = FakeKalshiClient(timeline)

        # Each API call reads from current event
        await client.get_positions()
        client.advance()  # move to next event
        await client.get_positions()  # reads from event[1]
    """

    def __init__(self, timeline: List[TimelineEvent]):
        self.timeline = timeline
        self._step = 0
        self.call_log: List[dict] = []

    @property
    def current(self) -> TimelineEvent:
        idx = min(self._step, len(self.timeline) - 1)
        return self.timeline[idx]

    def advance(self) -> None:
        if self._step < len(self.timeline) - 1:
            self._step += 1

    def reset(self) -> None:
        self._step = 0
        self.call_log.clear()

    async def get_positions(self, **kwargs) -> dict:
        self.call_log.append({"method": "get_positions", "kwargs": kwargs})
        event = self.current
        if event.verify_fails:
            raise Exception("Simulated API timeout")
        ticker_filter = kwargs.get("ticker")
        mps = []
        for p in event.positions:
            if ticker_filter and p.ticker != ticker_filter:
                continue
            mps.append({
                "ticker": p.ticker,
                "position_fp": str(p.position_fp),
                "total_traded_dollars": str(p.total_traded_dollars),
            })
        return {"market_positions": mps}

    async def get_market(self, ticker: str) -> dict:
        self.call_log.append({"method": "get_market", "ticker": ticker})
        event = self.current
        ms = event.markets.get(ticker, MarketStatus(ticker=ticker))
        return {
            "market": {
                "status": ms.status,
                "result": ms.result,
            }
        }

    async def create_order(self, **kwargs) -> dict:
        self.call_log.append({"method": "create_order", "kwargs": kwargs})
        event = self.current
        if event.order_result:
            return event.order_result
        return {"order": {"order_id": "fake-order-001", "status": "executed"}}

    async def get_order(self, order_id: str) -> dict:
        self.call_log.append({"method": "get_order", "order_id": order_id})
        return {"order": {"order_id": order_id, "status": "executed",
                          "fill_count_fp": "0"}}

    async def get_orders(self, **kwargs) -> dict:
        self.call_log.append({"method": "get_orders", "kwargs": kwargs})
        return {"orders": []}

    async def get_balance(self) -> dict:
        return {"balance": 10000}
