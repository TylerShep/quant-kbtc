"""Unit tests for the BUG-025 FillStream subscriber.

Covers:
  - Fill payload parsing (yes/no side, dollars/cents normalization)
  - VWAP / total cost / total fees aggregation across multi-fill orders
  - drain_for_order timeout, min_count gating, and per-order isolation
  - Buffer cap to prevent runaway memory if drain is missed
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from data.fill_stream import (
    Fill,
    FillStream,
    MAX_FILLS_PER_ORDER,
    _count_from_msg,
    _fee_cents_from_msg,
    _to_cents,
    _yes_price_from_msg,
)


@pytest.fixture(autouse=True)
def _stub_kalshi_auth():
    """KalshiAuth's __init__ tries to load a real PEM. In unit tests we
    only exercise FillStream's parsing/buffering logic, never the live
    socket, so a no-op auth object is fine."""
    with patch("data.fill_stream.KalshiAuth", autospec=True):
        yield


# ── Pure parser helpers ──────────────────────────────────────────────


def test_to_cents_handles_dollars_and_cents():
    assert _to_cents(0.25) == 25.0
    assert _to_cents("0.25") == 25.0
    assert _to_cents(25) == 25.0
    assert _to_cents("25") == 25.0
    assert _to_cents(None) is None
    assert _to_cents("garbage") is None
    assert _to_cents(-1) is None


def test_yes_price_parses_yes_side_dollars():
    msg = {"yes_price_dollars": "0.42"}
    assert _yes_price_from_msg(msg) == pytest.approx(42.0)


def test_yes_price_parses_yes_side_cents():
    msg = {"yes_price": 42}
    assert _yes_price_from_msg(msg) == pytest.approx(42.0)


def test_yes_price_falls_back_to_no_complement():
    msg = {"no_price_dollars": "0.30"}
    assert _yes_price_from_msg(msg) == pytest.approx(70.0)


def test_yes_price_returns_none_when_missing():
    assert _yes_price_from_msg({}) is None


def test_count_from_msg_uses_count_fp():
    assert _count_from_msg({"count_fp": "5.00"}) == 5
    assert _count_from_msg({"count": 7}) == 7
    assert _count_from_msg({}) == 0
    assert _count_from_msg({"count_fp": "garbage"}) == 0


def test_fee_cents_from_msg_dollars_and_cents():
    assert _fee_cents_from_msg({"fee_cost": "0.10"}) == pytest.approx(10.0)
    assert _fee_cents_from_msg({"fee_cost": "10"}) == pytest.approx(10.0)
    assert _fee_cents_from_msg({}) == 0.0


# ── Aggregation helpers ──────────────────────────────────────────────


def _f(order_id: str, *, count: int, yes_cents: float, side: str = "yes",
       action: str = "buy", fee_cents: float = 0.0) -> Fill:
    return Fill(
        trade_id=f"t-{order_id}-{count}-{yes_cents}",
        order_id=order_id,
        ticker="KXBTC-TEST",
        side=side,
        action=action,
        yes_price_cents=yes_cents,
        count=count,
        fee_cents=fee_cents,
        is_taker=True,
        received_at=0.0,
    )


def test_vwap_yes_cents_weights_by_count():
    fills = [
        _f("o1", count=2, yes_cents=20.0),
        _f("o1", count=8, yes_cents=30.0),
    ]
    # (2*20 + 8*30) / 10 = 280/10 = 28
    assert FillStream.vwap_yes_cents(fills) == pytest.approx(28.0)


def test_vwap_yes_cents_returns_none_for_empty():
    assert FillStream.vwap_yes_cents([]) is None


def test_vwap_yes_cents_returns_none_when_total_zero():
    fills = [_f("o1", count=0, yes_cents=20.0)]
    assert FillStream.vwap_yes_cents(fills) is None


def test_total_fees_dollars_sums_fees():
    fills = [
        _f("o1", count=5, yes_cents=20.0, fee_cents=4.0),
        _f("o1", count=5, yes_cents=30.0, fee_cents=6.0),
    ]
    assert FillStream.total_fees_dollars(fills) == pytest.approx(0.10)


def test_total_cost_dollars_yes_buy():
    """Yes-side buy: cost = count * yes_price / 100."""
    fills = [
        _f("o1", count=2, yes_cents=20.0, side="yes", action="buy"),
        _f("o1", count=8, yes_cents=30.0, side="yes", action="buy"),
    ]
    # 2*0.20 + 8*0.30 = 0.40 + 2.40 = 2.80
    assert FillStream.total_cost_dollars(fills) == pytest.approx(2.80)


def test_total_cost_dollars_no_buy_uses_complement():
    """No-side buy: cost = count * (1 - yes_price/100)."""
    # If yes is 30c, then no is 70c. We bought 5 No contracts.
    fills = [_f("o1", count=5, yes_cents=30.0, side="no", action="buy")]
    # 5 * (1 - 0.30) = 5 * 0.70 = 3.50
    assert FillStream.total_cost_dollars(fills) == pytest.approx(3.50)


def test_total_cost_dollars_yes_sell_same_sign():
    """Selling Yes also expressed as a positive dollar amount (proceeds)."""
    fills = [_f("o1", count=4, yes_cents=40.0, side="yes", action="sell")]
    assert FillStream.total_cost_dollars(fills) == pytest.approx(1.60)


# ── drain_for_order ──────────────────────────────────────────────────


def _seed_buffer(fs: FillStream, order_id: str, fills: list[Fill]) -> None:
    """Bypass the WS and drop fills directly into the per-order buffer."""
    for fill in fills:
        fs._fills_by_order[order_id].append(fill)


@pytest.mark.asyncio
async def test_drain_returns_buffered_fills_immediately():
    fs = FillStream()
    _seed_buffer(fs, "ord-A", [
        _f("ord-A", count=3, yes_cents=25.0),
        _f("ord-A", count=2, yes_cents=27.0),
    ])
    fills = await fs.drain_for_order("ord-A", min_count=5, timeout_sec=0.1)
    assert sum(f.count for f in fills) == 5
    # Buffer is cleared after drain
    assert "ord-A" not in fs._fills_by_order


@pytest.mark.asyncio
async def test_drain_returns_empty_when_no_fills_received():
    fs = FillStream()
    fills = await fs.drain_for_order("missing", min_count=5, timeout_sec=0.05)
    assert fills == []


@pytest.mark.asyncio
async def test_drain_isolates_per_order():
    fs = FillStream()
    _seed_buffer(fs, "ord-A", [_f("ord-A", count=4, yes_cents=20.0)])
    _seed_buffer(fs, "ord-B", [_f("ord-B", count=2, yes_cents=80.0)])
    a = await fs.drain_for_order("ord-A", min_count=4, timeout_sec=0.05)
    b = await fs.drain_for_order("ord-B", min_count=2, timeout_sec=0.05)
    assert sum(f.count for f in a) == 4
    assert sum(f.count for f in b) == 2
    # Each drain only removed its own order's buffer
    assert "ord-A" not in fs._fills_by_order
    assert "ord-B" not in fs._fills_by_order


@pytest.mark.asyncio
async def test_drain_returns_partial_at_timeout():
    """If min_count never satisfied, drain still returns whatever arrived."""
    fs = FillStream()
    _seed_buffer(fs, "ord-A", [_f("ord-A", count=2, yes_cents=20.0)])
    fills = await fs.drain_for_order("ord-A", min_count=5, timeout_sec=0.05)
    assert sum(f.count for f in fills) == 2


@pytest.mark.asyncio
async def test_drain_polls_for_late_arrivals():
    """Fills delivered while we're waiting are picked up before timeout."""
    fs = FillStream()
    _seed_buffer(fs, "ord-A", [_f("ord-A", count=2, yes_cents=20.0)])

    async def add_late_fill():
        await asyncio.sleep(0.05)
        async with fs._lock:
            fs._fills_by_order["ord-A"].append(_f("ord-A", count=3, yes_cents=21.0))

    add_task = asyncio.create_task(add_late_fill())
    fills = await fs.drain_for_order("ord-A", min_count=5, timeout_sec=1.0)
    await add_task
    assert sum(f.count for f in fills) == 5


@pytest.mark.asyncio
async def test_drain_zero_min_count_short_circuits():
    """min_count=0 returns immediately, even if buffer has data."""
    fs = FillStream()
    _seed_buffer(fs, "ord-A", [_f("ord-A", count=1, yes_cents=20.0)])
    fills = await fs.drain_for_order("ord-A", min_count=0, timeout_sec=0.5)
    # Returns whatever is buffered (1) without waiting
    assert sum(f.count for f in fills) == 1


@pytest.mark.asyncio
async def test_drain_blank_order_id_returns_empty_fast():
    fs = FillStream()
    fills = await fs.drain_for_order("", min_count=5, timeout_sec=2.0)
    assert fills == []


# ── Buffer cap ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_buffer_capped_at_max():
    """Even if Kalshi spams, buffer stays bounded."""
    fs = FillStream()
    for i in range(MAX_FILLS_PER_ORDER + 50):
        fs._fills_by_order["spammy"].append(_f("spammy", count=1, yes_cents=20.0))
    assert len(fs._fills_by_order["spammy"]) == MAX_FILLS_PER_ORDER


# ── _handle_message routing ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_message_buffers_fills():
    fs = FillStream()
    msg = {
        "type": "fill",
        "msg": {
            "trade_id": "t-1",
            "order_id": "ord-X",
            "market_ticker": "KXBTC-T",
            "side": "yes",
            "action": "buy",
            "yes_price_dollars": "0.45",
            "count_fp": "3.00",
            "fee_cost": "0.06",
            "is_taker": True,
        },
    }
    await fs._handle_message(msg)
    assert "ord-X" in fs._fills_by_order
    fill = fs._fills_by_order["ord-X"][0]
    assert fill.yes_price_cents == pytest.approx(45.0)
    assert fill.count == 3
    assert fill.fee_cents == pytest.approx(6.0)
    assert fill.side == "yes"
    assert fill.action == "buy"


@pytest.mark.asyncio
async def test_handle_message_ignores_non_fill_types():
    fs = FillStream()
    await fs._handle_message({"type": "subscribed", "sid": 1})
    assert dict(fs._fills_by_order) == {}


@pytest.mark.asyncio
async def test_handle_message_skips_zero_count():
    fs = FillStream()
    msg = {
        "type": "fill",
        "msg": {
            "order_id": "ord-Y",
            "yes_price_dollars": "0.50",
            "count_fp": "0",
        },
    }
    await fs._handle_message(msg)
    assert "ord-Y" not in fs._fills_by_order


@pytest.mark.asyncio
async def test_handle_message_skips_missing_order_id():
    fs = FillStream()
    msg = {"type": "fill", "msg": {"yes_price_dollars": "0.50", "count_fp": "1"}}
    await fs._handle_message(msg)
    assert dict(fs._fills_by_order) == {}


@pytest.mark.asyncio
async def test_handle_message_skips_missing_price():
    fs = FillStream()
    msg = {"type": "fill", "msg": {"order_id": "ord-Z", "count_fp": "1"}}
    await fs._handle_message(msg)
    assert "ord-Z" not in fs._fills_by_order
