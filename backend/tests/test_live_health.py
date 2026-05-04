"""Unit tests for backend/monitoring/live_health.py.

Each alarm is tested as a pure decision (the ``_should_fire`` helper)
plus an integration test against a fake pool that records what was
queried and what was written back to ``bot_state``. No real DB.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from monitoring import live_health


# ─── Fake DB pool ─────────────────────────────────────────────────────────

class FakeRow:
    """Fake psycopg/asyncpg row result that supports fetchone/fetchall."""

    def __init__(self, payload):
        self._payload = payload

    async def fetchone(self):
        if isinstance(self._payload, list):
            return self._payload[0] if self._payload else None
        return self._payload

    async def fetchall(self):
        if isinstance(self._payload, list):
            return self._payload
        return [self._payload] if self._payload else []


class FakeConn:
    """Records every query and returns canned rows in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.executed = []
        self._upserts = []

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        sql_upper = sql.strip().upper()
        if sql_upper.startswith("INSERT") or sql_upper.startswith("UPDATE"):
            self._upserts.append((sql, params))
            return FakeRow(None)
        if not self.responses:
            return FakeRow(None)
        return FakeRow(self.responses.pop(0))


class FakePool:
    """A pool that hands out one FakeConn per query batch."""

    def __init__(self, responses):
        self.conn = FakeConn(responses)

    @asynccontextmanager
    async def connection(self):
        yield self.conn


def _make_pool_for_drought(*, last_live_ts, paper_count, bot_state=None):
    """Pool that satisfies _query_drought_inputs + bot_state read+write."""
    responses = [
        (last_live_ts,),
        (paper_count,),
    ]
    if bot_state is not None:
        responses.append((json.dumps(bot_state),))
    else:
        responses.append(None)
    return FakePool(responses)


# ─── Drought: pure decision tests ─────────────────────────────────────────

NOW = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)


def test_drought_fires_when_age_exceeds_threshold():
    fires, age = live_health._drought_should_fire(
        last_live_ts=NOW - timedelta(hours=48),
        paper_count_36h=20, trading_mode="live",
        trading_paused="off", now=NOW,
    )
    assert fires is True
    assert age > 36


def test_drought_does_not_fire_below_threshold():
    fires, age = live_health._drought_should_fire(
        last_live_ts=NOW - timedelta(hours=12),
        paper_count_36h=20, trading_mode="live",
        trading_paused="off", now=NOW,
    )
    assert fires is False
    assert age == 12


def test_drought_requires_paper_activity():
    """Bot dark and paper also dark = no alarm. Paper inactivity means
    the signal may genuinely not be present, not that the live filter is
    over-blocking."""
    fires, _ = live_health._drought_should_fire(
        last_live_ts=NOW - timedelta(hours=72),
        paper_count_36h=2, trading_mode="live",
        trading_paused="off", now=NOW,
    )
    assert fires is False


def test_drought_does_not_fire_in_paper_mode():
    fires, _ = live_health._drought_should_fire(
        last_live_ts=NOW - timedelta(hours=72),
        paper_count_36h=20, trading_mode="paper",
        trading_paused="off", now=NOW,
    )
    assert fires is False


def test_drought_does_not_fire_when_paused():
    fires, _ = live_health._drought_should_fire(
        last_live_ts=NOW - timedelta(hours=72),
        paper_count_36h=20, trading_mode="live",
        trading_paused="paused", now=NOW,
    )
    assert fires is False


def test_drought_fires_with_no_live_history_at_all():
    """Bot has been in live mode and paper is firing but live has never
    traded. Worst case — fire immediately."""
    fires, age = live_health._drought_should_fire(
        last_live_ts=None, paper_count_36h=20,
        trading_mode="live", trading_paused="off", now=NOW,
    )
    assert fires is True
    assert age is None


def test_drought_handles_naive_datetime():
    """Some DB drivers return naive datetimes — code must coerce to UTC
    rather than crash."""
    naive = datetime(2026, 4, 27, 12, 0)
    fires, _ = live_health._drought_should_fire(
        last_live_ts=naive, paper_count_36h=20,
        trading_mode="live", trading_paused="off", now=NOW,
    )
    assert fires is True


# ─── Drought: integration with FakePool ───────────────────────────────────

@pytest.mark.asyncio
async def test_drought_alarm_posts_and_persists_cooldown():
    pool = _make_pool_for_drought(
        last_live_ts=NOW - timedelta(hours=72), paper_count=20,
    )
    notifier = MagicMock()
    notifier.live_drought_alarm = AsyncMock()

    await live_health.check_live_drought(
        pool, notifier, trading_mode="live", trading_paused="off", now=NOW,
    )

    notifier.live_drought_alarm.assert_awaited_once()
    upserts = pool.conn._upserts
    assert any("INSERT INTO bot_state" in s for s, _ in upserts), \
        "Cooldown state must be persisted after firing."


@pytest.mark.asyncio
async def test_drought_alarm_respects_cooldown():
    """Alarm fired 1h ago — within 12h cooldown, must not re-fire."""
    state = {"last_fired": (NOW - timedelta(hours=1)).isoformat()}
    pool = _make_pool_for_drought(
        last_live_ts=NOW - timedelta(hours=72), paper_count=20, bot_state=state,
    )
    notifier = MagicMock()
    notifier.live_drought_alarm = AsyncMock()

    await live_health.check_live_drought(
        pool, notifier, trading_mode="live", trading_paused="off", now=NOW,
    )
    notifier.live_drought_alarm.assert_not_called()


@pytest.mark.asyncio
async def test_drought_alarm_re_fires_after_cooldown():
    """13h since last fire — cooldown elapsed, alarm must fire again."""
    state = {"last_fired": (NOW - timedelta(hours=13)).isoformat()}
    pool = _make_pool_for_drought(
        last_live_ts=NOW - timedelta(hours=72), paper_count=20, bot_state=state,
    )
    notifier = MagicMock()
    notifier.live_drought_alarm = AsyncMock()

    await live_health.check_live_drought(
        pool, notifier, trading_mode="live", trading_paused="off", now=NOW,
    )
    notifier.live_drought_alarm.assert_awaited_once()


# ─── Skip-ratio: pure decision tests ──────────────────────────────────────

def test_skip_ratio_fires_on_two_consecutive_breaches():
    fires, n = live_health._skip_ratio_should_fire([0.10, 0.96, 0.97])
    assert fires is True
    assert n == 2


def test_skip_ratio_does_not_fire_on_single_breach():
    fires, n = live_health._skip_ratio_should_fire([0.10, 0.50, 0.97])
    assert fires is False
    assert n == 1


def test_skip_ratio_does_not_fire_below_threshold():
    fires, n = live_health._skip_ratio_should_fire([0.85, 0.90, 0.94])
    assert fires is False
    assert n == 0


def test_skip_ratio_requires_history_length():
    fires, _ = live_health._skip_ratio_should_fire([0.99])
    assert fires is False


def test_skip_ratio_continues_streak():
    fires, n = live_health._skip_ratio_should_fire([0.99, 0.99, 0.99, 0.99])
    assert fires is True
    assert n == 4


# ─── Skip-ratio: integration ──────────────────────────────────────────────

def _make_pool_for_skip_ratio(
    *, edge_count, total_count, top_reasons=None, bot_state=None,
):
    responses = [
        (edge_count, total_count),
        top_reasons or [],
    ]
    if bot_state is not None:
        responses.append((json.dumps(bot_state),))
    else:
        responses.append(None)
    return FakePool(responses)


@pytest.mark.asyncio
async def test_skip_ratio_appends_history_without_firing_on_first_breach():
    pool = _make_pool_for_skip_ratio(edge_count=970, total_count=1000)
    notifier = MagicMock()
    notifier.edge_skip_ratio_alarm = AsyncMock()

    await live_health.check_edge_skip_ratio(
        pool, notifier, trading_mode="live", now=NOW,
    )
    notifier.edge_skip_ratio_alarm.assert_not_called()
    upserts = pool.conn._upserts
    assert len(upserts) == 1
    saved = json.loads(upserts[0][1][1])
    assert saved["history"] == [0.97]


@pytest.mark.asyncio
async def test_skip_ratio_fires_on_second_consecutive_breach():
    state = {"history": [0.97]}
    pool = _make_pool_for_skip_ratio(
        edge_count=980, total_count=1000,
        top_reasons=[("EDGE_SHORT_BLOCKED", 750)],
        bot_state=state,
    )
    notifier = MagicMock()
    notifier.edge_skip_ratio_alarm = AsyncMock()

    await live_health.check_edge_skip_ratio(
        pool, notifier, trading_mode="live", now=NOW,
    )
    notifier.edge_skip_ratio_alarm.assert_awaited_once()


@pytest.mark.asyncio
async def test_skip_ratio_skipped_below_min_total_rows():
    """Don't draw conclusions from <100 signal_log rows in the window."""
    pool = _make_pool_for_skip_ratio(edge_count=50, total_count=50)
    notifier = MagicMock()
    notifier.edge_skip_ratio_alarm = AsyncMock()

    await live_health.check_edge_skip_ratio(
        pool, notifier, trading_mode="live", now=NOW,
    )
    notifier.edge_skip_ratio_alarm.assert_not_called()


@pytest.mark.asyncio
async def test_skip_ratio_no_op_in_paper_mode():
    pool = _make_pool_for_skip_ratio(edge_count=999, total_count=1000)
    notifier = MagicMock()
    notifier.edge_skip_ratio_alarm = AsyncMock()
    await live_health.check_edge_skip_ratio(
        pool, notifier, trading_mode="paper", now=NOW,
    )
    notifier.edge_skip_ratio_alarm.assert_not_called()


# ─── Imbalance: pure decision tests ───────────────────────────────────────

def test_imbalance_fires_when_short_dominates_and_no_live_shorts():
    fires = live_health._imbalance_should_fire(
        short_rejected=8000, long_rejected=100, live_short_count=0,
    )
    assert fires is True


def test_imbalance_does_not_fire_when_live_shorts_happened():
    """If shorts have actually traded live, the filter is working — it's
    just selective. No alarm."""
    fires = live_health._imbalance_should_fire(
        short_rejected=8000, long_rejected=100, live_short_count=3,
    )
    assert fires is False


def test_imbalance_does_not_fire_below_min_short_rejections():
    """Tiny absolute counts — could be a quiet day, not a filter problem."""
    fires = live_health._imbalance_should_fire(
        short_rejected=10, long_rejected=0, live_short_count=0,
    )
    assert fires is False


def test_imbalance_does_not_fire_below_ratio_threshold():
    fires = live_health._imbalance_should_fire(
        short_rejected=200, long_rejected=100, live_short_count=0,
    )
    assert fires is False


def test_imbalance_fires_on_zero_long_rejections_with_high_short():
    """Avoid div-by-zero — when long==0 and short is meaningfully high, fire."""
    fires = live_health._imbalance_should_fire(
        short_rejected=500, long_rejected=0, live_short_count=0,
    )
    assert fires is True


# ─── Imbalance: integration ───────────────────────────────────────────────

def _make_pool_for_imbalance(
    *, short_rejected, long_rejected, live_short_count, bot_state=None,
):
    responses = [
        (short_rejected, long_rejected),
        (live_short_count,),
    ]
    if bot_state is not None:
        responses.append((json.dumps(bot_state),))
    else:
        responses.append(None)
    return FakePool(responses)


@pytest.mark.asyncio
async def test_imbalance_alarm_fires_and_persists_cooldown():
    pool = _make_pool_for_imbalance(
        short_rejected=8000, long_rejected=100, live_short_count=0,
    )
    notifier = MagicMock()
    notifier.direction_imbalance_alarm = AsyncMock()

    await live_health.check_direction_imbalance(
        pool, notifier, trading_mode="live", now=NOW,
    )
    notifier.direction_imbalance_alarm.assert_awaited_once()
    upserts = pool.conn._upserts
    assert any("INSERT INTO bot_state" in s for s, _ in upserts)


@pytest.mark.asyncio
async def test_imbalance_alarm_respects_24h_cooldown():
    state = {"last_fired": (NOW - timedelta(hours=10)).isoformat()}
    pool = _make_pool_for_imbalance(
        short_rejected=8000, long_rejected=100, live_short_count=0,
        bot_state=state,
    )
    notifier = MagicMock()
    notifier.direction_imbalance_alarm = AsyncMock()

    await live_health.check_direction_imbalance(
        pool, notifier, trading_mode="live", now=NOW,
    )
    notifier.direction_imbalance_alarm.assert_not_called()


@pytest.mark.asyncio
async def test_imbalance_no_op_in_paper_mode():
    pool = _make_pool_for_imbalance(
        short_rejected=8000, long_rejected=100, live_short_count=0,
    )
    notifier = MagicMock()
    notifier.direction_imbalance_alarm = AsyncMock()

    await live_health.check_direction_imbalance(
        pool, notifier, trading_mode="paper", now=NOW,
    )
    notifier.direction_imbalance_alarm.assert_not_called()


# ─── Cooldown helper edge cases ───────────────────────────────────────────

def test_cooldown_handles_missing_iso():
    assert live_health._within_cooldown(None, 12, NOW) is False


def test_cooldown_handles_malformed_iso():
    assert live_health._within_cooldown("not-a-date", 12, NOW) is False


def test_cooldown_inside_window():
    one_hour_ago = (NOW - timedelta(hours=1)).isoformat()
    assert live_health._within_cooldown(one_hour_ago, 12, NOW) is True


def test_cooldown_outside_window():
    long_ago = (NOW - timedelta(hours=20)).isoformat()
    assert live_health._within_cooldown(long_ago, 12, NOW) is False
