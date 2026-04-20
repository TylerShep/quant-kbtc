"""Tests for BUG-022: phantom-entry race fixes in PositionManager.enter().

Three fixes covered:
- Fix A: Cancel-on-poll-timeout — when an entry "market" order rests on the
  Kalshi book (price field acts as a limit floor), cancel it before going
  FLAT so it can't match later and become an orphan.
- Fix B: Use _verify_with_retry on the entry path so a transient stale
  positions-endpoint read doesn't push us into phantom_entry_prevented.
- Fix C: Early bailout in _poll_order_fill when status="resting" so we
  cancel within ~3s instead of ~17s.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from execution.position_manager import (
    ENTRY_CANCEL_SETTLE_SEC,
    ENTRY_REST_BAILOUT_SEC,
    FILL_POLL_INTERVAL,
    PositionManager,
    PositionState,
)


# ── Speed-up fixture ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fast_clocks():
    """Shrink all real-time waits inside enter() so tests run in <1s.

    These constants are read at runtime by enter()/_poll_order_fill, so
    patching them in the position_manager module is sufficient — no need
    to re-import the SUT. We also stub asyncio.sleep within the module
    namespace so _verify_with_retry's exponential backoff doesn't add
    real-time delays during VERIFY_FAILED retries.
    """
    async def _instant_sleep(_seconds: float) -> None:
        return None

    with patch("execution.position_manager.ENTRY_REST_BAILOUT_SEC", 0.0), \
         patch("execution.position_manager.ENTRY_CANCEL_SETTLE_SEC", 0.0), \
         patch("execution.position_manager.FILL_POLL_INTERVAL", 0.001), \
         patch("execution.position_manager.FILL_POLL_TIMEOUT", 0.05), \
         patch("execution.position_manager.asyncio.sleep", _instant_sleep):
        yield


# ── Helpers ──────────────────────────────────────────────────────────


def _make_pm() -> PositionManager:
    client = MagicMock()
    return PositionManager(client)


def _order_resp(order_id: str = "ord-1") -> dict:
    return {"order": {"order_id": order_id}}


def _get_order_resp(
    *,
    status: str,
    filled: int = 0,
    yes_price_dollars: str | None = None,
    taker_fill_cost_dollars: str | None = None,
    taker_fees_dollars: str | None = None,
) -> dict:
    """Build a Kalshi /portfolio/orders/{id} response payload."""
    order: dict[str, Any] = {"status": status, "fill_count_fp": str(filled)}
    if yes_price_dollars is not None:
        order["yes_price_dollars"] = yes_price_dollars
    if taker_fill_cost_dollars is not None:
        order["taker_fill_cost_dollars"] = taker_fill_cost_dollars
    if taker_fees_dollars is not None:
        order["taker_fees_dollars"] = taker_fees_dollars
    return {"order": order}


def _positions_resp(ticker: str, position_fp: float) -> dict:
    return {
        "market_positions": [
            {"ticker": ticker, "position_fp": str(position_fp),
             "total_traded_dollars": "0"}
        ]
    }


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    response = httpx.Response(status_code=status_code, content=b"{}")
    request = httpx.Request("DELETE", "https://example/x")
    return httpx.HTTPStatusError("err", request=request, response=response)


# ══════════════════════════════════════════════════════════════════════
# Fix C: early bailout when status="resting"
# ══════════════════════════════════════════════════════════════════════


class TestEarlyRestBailout:

    async def test_resting_status_short_circuits_poll(self):
        """Poll loop should return as soon as status="resting" past bailout sec."""
        pm = _make_pm()
        # Always resting — without bailout this would loop FILL_POLL_TIMEOUT seconds.
        pm.client.get_order = AsyncMock(
            return_value=_get_order_resp(status="resting", filled=0),
        )
        # Bailout at 0 sec means "first poll wins". In production we use
        # ENTRY_REST_BAILOUT_SEC=2.0; using 0 here keeps the test fast.
        order_data = await pm._poll_order_fill(
            "ord-x", early_rest_bailout_sec=0.0,
        )
        assert order_data.get("status") == "resting"
        # Should have called get_order exactly once (returned on first poll).
        assert pm.client.get_order.call_count == 1

    async def test_terminal_status_returns_immediately_even_with_bailout(self):
        pm = _make_pm()
        pm.client.get_order = AsyncMock(
            return_value=_get_order_resp(status="executed", filled=5),
        )
        order_data = await pm._poll_order_fill(
            "ord-x", early_rest_bailout_sec=0.0,
        )
        assert order_data.get("status") == "executed"
        assert pm.client.get_order.call_count == 1

    async def test_no_bailout_arg_uses_legacy_behavior(self):
        """Without bailout arg, resting orders cause poll-timeout (legacy)."""
        pm = _make_pm()
        # Use a tiny patch so we don't actually wait 15s.
        from unittest.mock import patch
        with patch("execution.position_manager.FILL_POLL_TIMEOUT", 0.5):
            pm.client.get_order = AsyncMock(
                return_value=_get_order_resp(status="resting", filled=0),
            )
            await pm._poll_order_fill("ord-x")  # no bailout arg
        # Should have looped multiple times before timeout.
        assert pm.client.get_order.call_count >= 2


# ══════════════════════════════════════════════════════════════════════
# Fix A: cancel-on-timeout safe helper
# ══════════════════════════════════════════════════════════════════════


class TestCancelEntryOrderSafely:

    async def test_cancel_succeeds(self):
        pm = _make_pm()
        pm.client.cancel_order = AsyncMock(return_value={"order": {"status": "canceled"}})
        ok = await pm._cancel_entry_order_safely("ord-x", "KXBTC-T")
        assert ok is True
        pm.client.cancel_order.assert_called_once_with("ord-x")

    async def test_404_treated_as_already_terminal(self):
        pm = _make_pm()
        pm.client.cancel_order = AsyncMock(side_effect=_http_error(404))
        ok = await pm._cancel_entry_order_safely("ord-x", "KXBTC-T")
        assert ok is True

    async def test_410_treated_as_already_terminal(self):
        pm = _make_pm()
        pm.client.cancel_order = AsyncMock(side_effect=_http_error(410))
        ok = await pm._cancel_entry_order_safely("ord-x", "KXBTC-T")
        assert ok is True

    async def test_other_http_error_returns_false(self):
        pm = _make_pm()
        pm.client.cancel_order = AsyncMock(side_effect=_http_error(500))
        ok = await pm._cancel_entry_order_safely("ord-x", "KXBTC-T")
        assert ok is False

    async def test_arbitrary_exception_returns_false(self):
        pm = _make_pm()
        pm.client.cancel_order = AsyncMock(side_effect=RuntimeError("boom"))
        ok = await pm._cancel_entry_order_safely("ord-x", "KXBTC-T")
        assert ok is False


# ══════════════════════════════════════════════════════════════════════
# Fix B: _verify_with_retry on entry path
# ══════════════════════════════════════════════════════════════════════


class TestVerifyWithRetryUsedByEntry:

    async def test_retries_on_transient_failure_then_finds_position(self):
        """One verify_failed then a real position should still be adopted."""
        pm = _make_pm()
        ticker = "KXBTC-26APR1920-B73950"
        # get_positions sequence: pre-entry-flat, fail, then 4 contracts
        pm.client.get_positions = AsyncMock(side_effect=[
            {"market_positions": []},        # pre-entry
            Exception("boom"),                # verify retry 1 → VERIFY_FAILED
            _positions_resp(ticker, 4.0),    # verify retry 2 → 4 contracts
        ])
        pm.client.create_order = AsyncMock(return_value=_order_resp("ord-1"))
        # Order rests, then we cancel, then post-cancel poll says rested still
        # (cancel race with fill). Verify is the source of truth.
        pm.client.get_order = AsyncMock(side_effect=[
            _get_order_resp(status="resting", filled=0),     # initial poll → bailout
            _get_order_resp(status="canceled", filled=4,     # post-cancel poll
                            yes_price_dollars="0.15",
                            taker_fill_cost_dollars="0.60",
                            taker_fees_dollars="0.06"),
        ])
        pm.client.cancel_order = AsyncMock(return_value={})

        result = await pm.enter(
            ticker=ticker, direction="long", contracts=5, price=15,
            conviction="NORMAL", regime="MEDIUM",
        )

        assert result is not None
        assert result.contracts == 4
        assert pm.state == PositionState.OPEN
        # Confirm at least one retry happened on the verify path.
        assert pm.client.get_positions.call_count == 3


# ══════════════════════════════════════════════════════════════════════
# End-to-end: phantom-entry scenario from BUG-022
# ══════════════════════════════════════════════════════════════════════


class TestEnterRestThenCancelFlows:

    async def test_resting_then_truly_no_fill_goes_flat_with_cancel(self):
        """Order rests → cancel → verify=0 → FLAT. Cancel must have been called."""
        pm = _make_pm()
        ticker = "KXBTC-T"
        pm.client.create_order = AsyncMock(return_value=_order_resp("ord-1"))
        pm.client.get_order = AsyncMock(side_effect=[
            _get_order_resp(status="resting", filled=0),     # poll bailout
            _get_order_resp(status="canceled", filled=0),    # post-cancel re-poll
        ])
        pm.client.cancel_order = AsyncMock(return_value={})
        pm.client.get_positions = AsyncMock(side_effect=[
            {"market_positions": []},         # pre-entry flat
            {"market_positions": []},         # verify post-cancel: 0
        ])

        result = await pm.enter(
            ticker=ticker, direction="long", contracts=5, price=15,
            conviction="NORMAL", regime="MEDIUM",
        )

        assert result is None
        assert pm.state == PositionState.FLAT
        pm.client.cancel_order.assert_called_once_with("ord-1")

    async def test_resting_then_silent_fill_after_cancel_adopts_position(self):
        """Order rests → cancel races with fill → verify=4 → adopt OPEN with 4."""
        pm = _make_pm()
        ticker = "KXBTC-T"
        pm.client.create_order = AsyncMock(return_value=_order_resp("ord-1"))
        pm.client.get_order = AsyncMock(side_effect=[
            _get_order_resp(status="resting", filled=0),     # initial bailout
            _get_order_resp(status="canceled", filled=0),    # post-cancel poll: still 0
            _get_order_resp(status="canceled", filled=4,     # final recovery fetch
                            yes_price_dollars="0.15",
                            taker_fill_cost_dollars="0.60",
                            taker_fees_dollars="0.06"),
        ])
        pm.client.cancel_order = AsyncMock(return_value={})
        pm.client.get_positions = AsyncMock(side_effect=[
            {"market_positions": []},         # pre-entry flat
            _positions_resp(ticker, 4.0),     # verify post-cancel: 4 contracts
        ])

        result = await pm.enter(
            ticker=ticker, direction="long", contracts=5, price=15,
            conviction="NORMAL", regime="MEDIUM",
        )

        assert result is not None
        assert result.contracts == 4
        assert result.entry_price == 15.0  # parsed from yes_price_dollars=0.15
        assert pm.state == PositionState.OPEN
        # Cancel was issued, then the post-cancel-fill recovery fetch ran.
        pm.client.cancel_order.assert_called_once_with("ord-1")

    async def test_immediate_executed_fill_skips_cancel_path(self):
        """Happy path: order fills immediately, no cancel issued."""
        pm = _make_pm()
        ticker = "KXBTC-T"
        pm.client.create_order = AsyncMock(return_value=_order_resp("ord-1"))
        pm.client.get_order = AsyncMock(return_value=_get_order_resp(
            status="executed", filled=5,
            yes_price_dollars="0.20",
            taker_fill_cost_dollars="1.00",
            taker_fees_dollars="0.03",
        ))
        pm.client.cancel_order = AsyncMock()
        pm.client.get_positions = AsyncMock(side_effect=[
            {"market_positions": []},         # pre-entry flat
            _positions_resp(ticker, 5.0),     # verify
        ])

        result = await pm.enter(
            ticker=ticker, direction="long", contracts=5, price=20,
            conviction="NORMAL", regime="MEDIUM",
        )

        assert result is not None
        assert result.contracts == 5
        assert result.entry_price == 20.0
        assert pm.state == PositionState.OPEN
        pm.client.cancel_order.assert_not_called()

    async def test_canceled_zero_fill_no_cancel_issued(self):
        """If Kalshi reports the order canceled with 0 fill, no extra cancel needed."""
        pm = _make_pm()
        ticker = "KXBTC-T"
        pm.client.create_order = AsyncMock(return_value=_order_resp("ord-1"))
        pm.client.get_order = AsyncMock(return_value=_get_order_resp(
            status="canceled", filled=0,
        ))
        pm.client.cancel_order = AsyncMock()
        pm.client.get_positions = AsyncMock(side_effect=[
            {"market_positions": []},         # pre-entry flat
        ])

        result = await pm.enter(
            ticker=ticker, direction="long", contracts=5, price=15,
            conviction="NORMAL", regime="MEDIUM",
        )

        assert result is None
        assert pm.state == PositionState.FLAT
        pm.client.cancel_order.assert_not_called()

    async def test_partial_fill_with_resting_remainder_cancels_and_keeps_filled(self):
        """Order partially filled, remainder still resting: cancel resting, keep fill."""
        pm = _make_pm()
        ticker = "KXBTC-T"
        pm.client.create_order = AsyncMock(return_value=_order_resp("ord-1"))
        pm.client.get_order = AsyncMock(side_effect=[
            # First poll: 2 of 5 filled, status still resting (remainder)
            _get_order_resp(status="resting", filled=2,
                            yes_price_dollars="0.15",
                            taker_fill_cost_dollars="0.30",
                            taker_fees_dollars="0.03"),
            # After cancel: order canceled, total fill=2
            _get_order_resp(status="canceled", filled=2,
                            yes_price_dollars="0.15",
                            taker_fill_cost_dollars="0.30",
                            taker_fees_dollars="0.03"),
        ])
        pm.client.cancel_order = AsyncMock(return_value={})
        pm.client.get_positions = AsyncMock(side_effect=[
            {"market_positions": []},         # pre-entry flat
            _positions_resp(ticker, 2.0),     # verify
        ])

        result = await pm.enter(
            ticker=ticker, direction="long", contracts=5, price=15,
            conviction="NORMAL", regime="MEDIUM",
        )

        assert result is not None
        assert result.contracts == 2
        assert pm.state == PositionState.OPEN
        pm.client.cancel_order.assert_called_once_with("ord-1")

    async def test_cancel_404_is_swallowed_and_verify_decides(self):
        """cancel_order returning 404 (race: order already terminalized) must
        not abort the flow — verify decides whether we have a position."""
        pm = _make_pm()
        ticker = "KXBTC-T"
        pm.client.create_order = AsyncMock(return_value=_order_resp("ord-1"))
        pm.client.get_order = AsyncMock(side_effect=[
            _get_order_resp(status="resting", filled=0),
            # Cancel 404'd; post-cancel re-poll says executed with 5
            _get_order_resp(status="executed", filled=5,
                            yes_price_dollars="0.18",
                            taker_fill_cost_dollars="0.90",
                            taker_fees_dollars="0.05"),
        ])
        pm.client.cancel_order = AsyncMock(side_effect=_http_error(404))
        pm.client.get_positions = AsyncMock(side_effect=[
            {"market_positions": []},
            _positions_resp(ticker, 5.0),
        ])

        result = await pm.enter(
            ticker=ticker, direction="long", contracts=5, price=18,
            conviction="NORMAL", regime="MEDIUM",
        )

        assert result is not None
        assert result.contracts == 5
        assert pm.state == PositionState.OPEN

    async def test_resting_post_cancel_poll_fails_then_verify_zero_goes_flat(self):
        """Resilient to post-cancel poll API errors when verify=0."""
        pm = _make_pm()
        ticker = "KXBTC-T"
        pm.client.create_order = AsyncMock(return_value=_order_resp("ord-1"))
        pm.client.get_order = AsyncMock(side_effect=[
            _get_order_resp(status="resting", filled=0),
            Exception("transient post-cancel poll error"),
        ])
        pm.client.cancel_order = AsyncMock(return_value={})
        pm.client.get_positions = AsyncMock(side_effect=[
            {"market_positions": []},
            {"market_positions": []},  # verify confirms 0
        ])

        result = await pm.enter(
            ticker=ticker, direction="long", contracts=5, price=15,
            conviction="NORMAL", regime="MEDIUM",
        )

        assert result is None
        assert pm.state == PositionState.FLAT


# ══════════════════════════════════════════════════════════════════════
# Constant sanity checks
# ══════════════════════════════════════════════════════════════════════


class TestConstants:

    def test_rest_bailout_is_short(self):
        # Must be < FILL_POLL_TIMEOUT (15s) and reasonably tight to keep
        # the orphan-creation window small.
        assert 0 < ENTRY_REST_BAILOUT_SEC < 15.0
        assert ENTRY_REST_BAILOUT_SEC <= 5.0

    def test_cancel_settle_is_short(self):
        assert 0 < ENTRY_CANCEL_SETTLE_SEC <= 3.0

    def test_poll_interval_smaller_than_bailout(self):
        # We must be able to observe at least one poll within the bailout.
        assert FILL_POLL_INTERVAL <= ENTRY_REST_BAILOUT_SEC
