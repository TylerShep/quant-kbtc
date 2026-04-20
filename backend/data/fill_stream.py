"""
FillStream — authenticated Kalshi WebSocket subscriber for the user `fill`
channel.

Provides per-execution truth for the price/cost/fees of every order the bot
places. Used by `PositionManager` to compute volume-weighted average prices
(VWAP) and dollar-accurate costs, replacing the stale-prone polled order
response (BUG-025).

Architecture mirrors `KalshiWebSocketClient` in `kalshi_ws.py`:
  - Persistent reconnect with exponential backoff
  - RSA-signed auth via `KalshiAuth`
  - Subscribes once after connect; no per-ticker filter (the bot has at
    most one live position at a time so traffic is negligible)

Design notes
------------
  - The WS path is **purely additive**. If the stream is disconnected, in
    flight, or never receives a fill within `drain_for_order(timeout_sec)`,
    the consumer MUST fall back to its existing `_parse_fill_*` logic. No
    order-management decisions block on this stream.
  - We bound each per-order buffer at `MAX_FILLS_PER_ORDER` so a missed
    drain can't leak memory if Kalshi spams duplicate fills.
  - Yes-side prices are normalized to **cents** (0..100) regardless of
    whether Kalshi sent dollars or cents, so the consumer can mix VWAP
    output with the existing cents-based PnL math.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional

import structlog
import websockets

from config import settings
from data.kalshi_ws import KalshiAuth

logger = structlog.get_logger(__name__)


MAX_FILLS_PER_ORDER = 200


@dataclass(frozen=True)
class Fill:
    """One Kalshi fill execution.

    All prices are normalized to **Yes-side cents** (0..100) to match the
    rest of the codebase's PnL math.
    """
    trade_id: str
    order_id: str
    ticker: str
    side: str            # "yes" or "no" — which contract side was filled
    action: str          # "buy" or "sell"
    yes_price_cents: float
    count: int
    fee_cents: float
    is_taker: bool
    received_at: float   # local monotonic-ish wall time


def _to_cents(raw) -> Optional[float]:
    """Normalize a Kalshi price field (dollars or cents) to cents."""
    if raw is None:
        return None
    try:
        val = float(raw)
    except (ValueError, TypeError):
        return None
    if val < 0:
        return None
    if val < 1.0:
        return val * 100.0
    return val


def _yes_price_from_msg(msg: dict) -> Optional[float]:
    """Pull the Yes-side price in cents from a fill `msg` payload.

    Kalshi sends `yes_price_dollars`. Older payloads sometimes omit it but
    include `no_price_dollars`; we convert via 100 - no.
    """
    yes = _to_cents(msg.get("yes_price_dollars"))
    if yes is not None:
        return yes
    yes = _to_cents(msg.get("yes_price"))
    if yes is not None:
        return yes
    no = _to_cents(msg.get("no_price_dollars"))
    if no is None:
        no = _to_cents(msg.get("no_price"))
    if no is not None:
        return 100.0 - no
    return None


def _count_from_msg(msg: dict) -> int:
    """Parse the integer fill count from `count_fp` (fixed-point string)."""
    raw = msg.get("count_fp")
    if raw is None:
        raw = msg.get("count")
    if raw is None:
        return 0
    try:
        return int(round(float(raw)))
    except (ValueError, TypeError):
        return 0


def _fee_cents_from_msg(msg: dict) -> float:
    """Parse the fee in cents from `fee_cost` (fixed-point dollars)."""
    raw = msg.get("fee_cost")
    if raw is None:
        return 0.0
    try:
        val = float(raw)
    except (ValueError, TypeError):
        return 0.0
    return val * 100.0 if val < 1.0 else val


class FillStream:
    """Authenticated subscriber for the Kalshi `fill` channel.

    Public API:
      - ``await start()`` -- connect and begin consuming fills in the
        background.
      - ``await stop()`` -- close the socket; safe to call multiple times.
      - ``await drain_for_order(order_id, *, min_count, timeout_sec)`` --
        wait briefly for ``min_count`` contracts to be reported and return
        (and clear) every Fill we've buffered for that ``order_id``.
      - ``vwap_yes_cents(fills)`` / ``total_fees_dollars(fills)`` /
        ``total_cost_dollars(fills)`` -- pure aggregations a caller can
        run on the drained list.
      - ``connected`` -- boolean health, surfaced in `/api/status`.
    """

    DRAIN_POLL_INTERVAL = 0.05  # seconds

    def __init__(self) -> None:
        self.auth = KalshiAuth()
        self._fills_by_order: Dict[str, Deque[Fill]] = defaultdict(
            lambda: deque(maxlen=MAX_FILLS_PER_ORDER)
        )
        self._lock = asyncio.Lock()
        self._running = False
        self._ws = None
        self.connected = False
        self.last_message_time: Optional[float] = None
        self.message_count = 0
        self.connect_attempts = 0

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        asyncio.create_task(self._run_forever())
        logger.info("fill_stream.started")

    async def stop(self) -> None:
        self._running = False
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        self.connected = False
        logger.info("fill_stream.stopped")

    # ── public aggregation helpers ────────────────────────────────────

    @staticmethod
    def vwap_yes_cents(fills: List[Fill]) -> Optional[float]:
        """Volume-weighted average Yes-side price in cents.

        Returns None if `fills` is empty or total count is zero.
        """
        if not fills:
            return None
        total_count = sum(f.count for f in fills)
        if total_count <= 0:
            return None
        weighted = sum(f.yes_price_cents * f.count for f in fills)
        return weighted / total_count

    @staticmethod
    def total_fees_dollars(fills: List[Fill]) -> float:
        """Sum of all fees across the fills, in dollars."""
        return sum(f.fee_cents for f in fills) / 100.0

    @staticmethod
    def total_cost_dollars(fills: List[Fill]) -> float:
        """Total cash outlay (or proceeds) in dollars from the fills.

        Computes per-execution dollar amount as ``count * effective_price``
        where ``effective_price`` is:
          * the Yes-side price for ``side="yes"`` fills (we paid for Yes),
          * (1 - Yes price) for ``side="no"`` fills (we paid for No, which
            is the complement of Yes on a Kalshi binary contract).

        For ``action="buy"`` this is what we paid. For ``action="sell"``
        it's what we received. Sign is intentionally NOT flipped here --
        ``PositionManager`` already treats both ``entry_cost`` and
        ``exit_cost`` as positive dollar amounts spent/received and folds
        them into PnL via ``contracts_value - entry_cost - exit_cost``.
        """
        total = 0.0
        for f in fills:
            if f.side == "yes":
                eff = f.yes_price_cents
            else:
                eff = 100.0 - f.yes_price_cents
            total += (f.count * eff) / 100.0
        return total

    # ── drain ─────────────────────────────────────────────────────────

    async def drain_for_order(
        self,
        order_id: str,
        *,
        min_count: int,
        timeout_sec: float = 1.0,
    ) -> List[Fill]:
        """Return every Fill seen for ``order_id`` and clear the buffer.

        Polls up to ``timeout_sec`` for ``sum(count) >= min_count`` so the
        consumer can run concurrently with the existing 1.5s ledger-lag
        sleep without adding meaningful latency on the happy path.

        Always returns whatever we have at the deadline -- even if it's
        empty. The consumer is responsible for falling back to
        ``_parse_fill_*`` when this returns no matching fills.
        """
        if not order_id:
            return []
        deadline = time.monotonic() + max(timeout_sec, 0.0)
        while True:
            async with self._lock:
                buffered = list(self._fills_by_order.get(order_id, ()))
            total = sum(f.count for f in buffered)
            if total >= min_count:
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(self.DRAIN_POLL_INTERVAL)
        async with self._lock:
            self._fills_by_order.pop(order_id, None)
        return buffered

    # ── reconnect loop ────────────────────────────────────────────────

    async def _run_forever(self) -> None:
        from notifications import get_notifier
        attempt = 0
        while self._running:
            try:
                await self._connect()
                attempt = 0
            except Exception as e:
                attempt += 1
                wait = min(2 ** attempt, 60)
                logger.warning(
                    "fill_stream.disconnected",
                    error=str(e), retry_in=wait,
                )
                if attempt >= 3:
                    asyncio.create_task(get_notifier().ws_disconnected(
                        feed="KalshiFill",
                        error=str(e),
                        attempt=attempt,
                    ))
                if self._running:
                    await asyncio.sleep(wait)

    async def _connect(self) -> None:
        ts_ms = str(int(time.time() * 1000))
        signed_path = "/trade-api/ws/v2"
        sig = self.auth.sign(ts_ms + "GET" + signed_path)

        ws_headers = {
            "KALSHI-ACCESS-KEY": settings.kalshi.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        }

        self.connect_attempts += 1
        async with websockets.connect(
            settings.kalshi.ws_url,
            additional_headers=ws_headers,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            self._ws = ws
            self.connected = True
            await self._subscribe(ws)
            logger.info("fill_stream.connected")
            try:
                async for raw in ws:
                    if not self._running:
                        break
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    self.last_message_time = time.time()
                    self.message_count += 1
                    await self._handle_message(msg)
            finally:
                self.connected = False
                self._ws = None

    async def _subscribe(self, ws) -> None:
        await ws.send(json.dumps({
            "id": 1,
            "cmd": "subscribe",
            "params": {"channels": ["fill"]},
        }))

    # ── message handling ──────────────────────────────────────────────

    async def _handle_message(self, msg: dict) -> None:
        msg_type = msg.get("type", "")
        if msg_type != "fill":
            # Subscription/error frames; nothing to buffer.
            return
        payload = msg.get("msg") or {}
        order_id = payload.get("order_id")
        if not order_id:
            return

        yes_cents = _yes_price_from_msg(payload)
        if yes_cents is None:
            logger.warning(
                "fill_stream.fill_missing_price",
                order_id=order_id,
                payload_keys=list(payload.keys()),
            )
            return
        count = _count_from_msg(payload)
        if count <= 0:
            return

        fill = Fill(
            trade_id=str(payload.get("trade_id") or ""),
            order_id=str(order_id),
            ticker=str(payload.get("market_ticker") or ""),
            side=str(payload.get("side") or "yes").lower(),
            action=str(payload.get("action") or "buy").lower(),
            yes_price_cents=yes_cents,
            count=count,
            fee_cents=_fee_cents_from_msg(payload),
            is_taker=bool(payload.get("is_taker", True)),
            received_at=time.time(),
        )

        async with self._lock:
            self._fills_by_order[fill.order_id].append(fill)

        logger.info(
            "fill_stream.fill_buffered",
            order_id=fill.order_id,
            ticker=fill.ticker,
            side=fill.side,
            action=fill.action,
            yes_price_cents=round(fill.yes_price_cents, 4),
            count=fill.count,
            fee_cents=round(fill.fee_cents, 4),
        )
