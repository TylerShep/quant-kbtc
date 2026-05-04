"""Tests for the Discord-noise-control behavior of ``_schedule_tuning``.

The full ``_schedule_tuning`` is an infinite loop that pulls live DB data,
runs the walk-forward optimizer, and posts to Discord. Spinning it up in a
unit test is overkill. Instead we exercise the helper
``_maybe_post_tuning_daily_summary`` directly and validate the
post/no-post decision logic that lives in the loop body via a small
fake of the relevant inputs.

The fix being tested (2026-05-02): the bot was posting an "Auto-Tuner
Cycle Report" Discord embed every 6 hours saying "Walk-forward produced
no valid windows" because the bot only had ~2,200 BTC candles vs the
~4,000 needed for a single train+test window. After the fix we should:

  1. Skip Discord posts for no-op cycles (no apply, no health alerts,
     no changes).
  2. Track noop cycles in an in-memory counter.
  3. Post a single daily summary when a 24h period was 100% noop.
  4. Reset the counter the moment a real (non-noop) post fires.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backtesting.auto_tuner import TuningResult


def _arun(coro):
    """Run a coroutine in a fresh event loop. Avoids cross-test loop reuse."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_coord_stub():
    """Construct just the bound-method bag we need to test the helper.

    The helper is a pure async method that calls ``get_notifier()`` and
    logs. We don't need a real ``Coordinator`` to exercise it.
    """
    from coordinator import Coordinator
    return Coordinator.__new__(Coordinator)  # bypass __init__


# ─── _maybe_post_tuning_daily_summary ─────────────────────────────────────
def test_summary_skipped_when_same_day():
    """If we're still in the same UTC day as last_summary_date, the
    summary must NOT post regardless of the noop count."""
    coord = _make_coord_stub()
    today = datetime.now(timezone.utc).date()
    skipped = {"Walk-forward produced no valid windows": 4}

    fake_notifier = AsyncMock()
    fake_notifier.tuning_cycle_report = AsyncMock()
    with patch("coordinator.get_notifier", return_value=fake_notifier):
        _arun(coord._maybe_post_tuning_daily_summary(skipped, today))

    fake_notifier.tuning_cycle_report.assert_not_awaited()
    assert skipped == {"Walk-forward produced no valid windows": 4}, (
        "Same-day path must NOT clear the streak counter — it's still "
        "accumulating for today."
    )


def test_summary_skipped_when_no_noops():
    """An empty skipped_reasons dict means no noop cycles to summarize."""
    coord = _make_coord_stub()
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    skipped: dict[str, int] = {}

    fake_notifier = AsyncMock()
    fake_notifier.tuning_cycle_report = AsyncMock()
    with patch("coordinator.get_notifier", return_value=fake_notifier):
        _arun(coord._maybe_post_tuning_daily_summary(skipped, yesterday))

    fake_notifier.tuning_cycle_report.assert_not_awaited()


def test_summary_posts_once_when_day_rolled_with_noops():
    """When the UTC day has rolled over and the previous day had ≥1 noop
    cycle, post a single summary embed and clear the counter."""
    coord = _make_coord_stub()
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    skipped = {
        "Walk-forward produced no valid windows": 3,
        "No parameter changes needed": 1,
    }

    fake_notifier = AsyncMock()
    fake_notifier.tuning_cycle_report = AsyncMock()
    with patch("coordinator.get_notifier", return_value=fake_notifier):
        _arun(coord._maybe_post_tuning_daily_summary(skipped, yesterday))

    fake_notifier.tuning_cycle_report.assert_awaited_once()
    call = fake_notifier.tuning_cycle_report.await_args
    assert call.kwargs["should_apply"] is False
    assert call.kwargs["changes"] is None
    assert call.kwargs["health_alerts"] is None
    assert "4" in call.kwargs["reason"], (
        "Summary must include total noop count (3 + 1 = 4)"
    )
    assert "Walk-forward produced no valid windows" in call.kwargs["reason"], (
        "Summary must surface the most-common noop reason"
    )
    assert skipped == {}, (
        "Counter must be cleared after a successful post so the next day "
        "starts at 0."
    )


def test_summary_clears_counter_even_if_post_fails():
    """If the Discord webhook raises, we MUST still reset the counter.
    Otherwise a chronic webhook outage would leave the dict growing
    forever and the bot would post an ever-larger summary every day."""
    coord = _make_coord_stub()
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    skipped = {"some reason": 2}

    fake_notifier = AsyncMock()
    fake_notifier.tuning_cycle_report = AsyncMock(
        side_effect=RuntimeError("discord 503"),
    )
    with patch("coordinator.get_notifier", return_value=fake_notifier):
        _arun(coord._maybe_post_tuning_daily_summary(skipped, yesterday))

    fake_notifier.tuning_cycle_report.assert_awaited_once()
    assert skipped == {}, (
        "Even on Discord failure, the counter must be cleared in finally "
        "so we don't pile up indefinitely."
    )


# ─── No-op classification (the predicate that gates the in-loop post) ────
def _is_noop(result: TuningResult, health_alerts: list[str]) -> bool:
    """Mirror of the inline predicate inside ``_schedule_tuning``. Kept
    here so we can unit-test the precise definition of "noop"."""
    return (
        not result.should_apply
        and not health_alerts
        and not (result.changes or {})
    )


def test_no_valid_windows_is_noop():
    """The exact string the bot has been spamming. Must register as noop."""
    result = TuningResult(
        timestamp=0.0,
        current_params={},
        recommended_params={},
        edge_consistency=0.0,
        avg_oos_sharpe=0.0,
        should_apply=False,
        reason="Walk-forward produced no valid windows",
        changes={},
    )
    assert _is_noop(result, []) is True


def test_no_param_changes_is_noop():
    result = TuningResult(
        timestamp=0.0,
        current_params={"x": 1},
        recommended_params={"x": 1},
        edge_consistency=0.7,
        avg_oos_sharpe=1.2,
        should_apply=False,
        reason="No parameter changes needed",
        changes={},
    )
    assert _is_noop(result, []) is True


def test_health_alerts_alone_break_noop():
    """A signal-health alert is operator-actionable; never silence it
    even if the underlying tuning cycle had no recommended changes."""
    result = TuningResult(
        timestamp=0.0,
        current_params={},
        recommended_params={},
        edge_consistency=0.0,
        avg_oos_sharpe=0.0,
        should_apply=False,
        reason="Walk-forward produced no valid windows",
        changes={},
    )
    assert _is_noop(result, ["live drought 36h"]) is False


def test_should_apply_alone_breaks_noop():
    """An auto-applicable recommendation is the whole point of posting."""
    result = TuningResult(
        timestamp=0.0,
        current_params={"x": 1},
        recommended_params={"x": 2},
        edge_consistency=0.8,
        avg_oos_sharpe=1.5,
        should_apply=True,
        reason="Recommended: consistency=80%, OOS Sharpe=1.50",
        changes={"x": {"from": 1, "to": 2}},
    )
    assert _is_noop(result, []) is False


def test_changes_alone_break_noop():
    """Changes computed but should_apply=False (e.g. below sharpe floor):
    still informational; operator may want to know an edge is forming."""
    result = TuningResult(
        timestamp=0.0,
        current_params={"x": 1},
        recommended_params={"x": 2},
        edge_consistency=0.55,
        avg_oos_sharpe=0.7,  # below MIN_OOS_SHARPE
        should_apply=False,
        reason="OOS Sharpe 0.70 below threshold 0.80",
        changes={"x": {"from": 1, "to": 2}},
    )
    assert _is_noop(result, []) is False
