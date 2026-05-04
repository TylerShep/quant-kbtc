"""Live trading health tripwire alarms.

Three independent checks that catch the failure modes the bot is most
likely to hit silently:

  1. ``check_live_drought``      — live lane has gone too long without
                                    a trade despite paper firing.
  2. ``check_edge_skip_ratio``   — the edge_profile filter has been
                                    rejecting >95% of would-be entries
                                    for two consecutive checks.
  3. ``check_direction_imbalance`` — short-side rejections vastly
                                    outweigh long-side rejections AND
                                    no live shorts have happened in 7d
                                    (catches "filter is correct but
                                    market regime no longer fits").

Each check is a pure function of (pool, notifier, now) so it can be
unit-tested by faking the pool's fetch results.

Cooldowns are persisted in ``bot_state`` so they survive bot restarts.
That matters because the alarm scheduler runs every hour; without a
durable cooldown a restart would re-fire every alarm immediately.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)


DROUGHT_BOT_STATE_KEY = "live_drought_alarm"
SKIP_RATIO_BOT_STATE_KEY = "edge_skip_ratio_history"
IMBALANCE_BOT_STATE_KEY = "direction_imbalance_alarm"

DROUGHT_THRESHOLD_HOURS = 36
DROUGHT_PAPER_MIN_TRADES = 5
DROUGHT_COOLDOWN_HOURS = 12

SKIP_RATIO_THRESHOLD = 0.95
SKIP_RATIO_REQUIRED_CONSECUTIVE = 2
SKIP_RATIO_HISTORY_LIMIT = 7
SKIP_RATIO_COOLDOWN_HOURS = 12

IMBALANCE_RATIO = 5.0
IMBALANCE_MIN_SHORT_REJECTIONS = 50
IMBALANCE_COOLDOWN_HOURS = 24


async def _get_bot_state(pool, key: str) -> Optional[dict]:
    """Read a JSON value from bot_state. Returns None on miss/error."""
    try:
        async with pool.connection() as conn:
            row = await conn.execute(
                "SELECT value FROM bot_state WHERE key = %s", (key,),
            )
            result = await row.fetchone()
        if result is None:
            return None
        val = result[0]
        return val if isinstance(val, dict) else json.loads(val)
    except Exception as e:
        logger.warning("live_health.bot_state_read_failed", key=key, error=str(e))
        return None


async def _save_bot_state(pool, key: str, value: dict) -> None:
    """Upsert a JSON value into bot_state."""
    try:
        async with pool.connection() as conn:
            await conn.execute(
                """INSERT INTO bot_state (key, value, updated_at)
                   VALUES (%s, %s::jsonb, NOW())
                   ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value, updated_at = NOW()""",
                (key, json.dumps(value)),
            )
    except Exception as e:
        logger.error("live_health.bot_state_save_failed", key=key, error=str(e))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _within_cooldown(last_alarm_iso: Optional[str], cooldown_hours: int,
                     now: Optional[datetime] = None) -> bool:
    """True when the last alarm fired within the cooldown window."""
    if not last_alarm_iso:
        return False
    try:
        last = datetime.fromisoformat(last_alarm_iso)
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    cutoff = (now or _now()) - timedelta(hours=cooldown_hours)
    return last > cutoff


# ─── 1a. Live drought ─────────────────────────────────────────────────────

async def _query_drought_inputs(pool) -> dict:
    """Fetch the inputs the drought check needs: last live trade ts and
    paper trade count over the same lookback window.
    """
    try:
        async with pool.connection() as conn:
            row = await conn.execute(
                """SELECT MAX(timestamp) FROM trades
                   WHERE trading_mode = 'live'""",
            )
            last_live_row = await row.fetchone()
            last_live_ts = last_live_row[0] if last_live_row else None

            row = await conn.execute(
                """SELECT COUNT(*) FROM trades
                   WHERE trading_mode = 'paper'
                     AND timestamp > NOW() - INTERVAL '36 hours'""",
            )
            paper_count_row = await row.fetchone()
            paper_count = int(paper_count_row[0]) if paper_count_row else 0
    except Exception as e:
        logger.error("live_health.drought_query_failed", error=str(e))
        return {"last_live_ts": None, "paper_count_36h": 0, "error": str(e)}
    return {"last_live_ts": last_live_ts, "paper_count_36h": paper_count}


def _drought_should_fire(
    *,
    last_live_ts: Optional[datetime],
    paper_count_36h: int,
    trading_mode: str,
    trading_paused: str,
    now: datetime,
) -> tuple[bool, Optional[float]]:
    """Pure decision: is the drought condition met right now?

    Returns (fires, age_hours). ``age_hours`` is the live-trade staleness
    in hours (or None if there's never been a live trade).
    """
    if trading_mode != "live" or trading_paused != "off":
        return False, None
    if paper_count_36h < DROUGHT_PAPER_MIN_TRADES:
        return False, None
    if last_live_ts is None:
        # No live trade ever recorded but bot is in live mode and paper
        # is firing. That's the worst-case drought.
        return True, None
    if last_live_ts.tzinfo is None:
        last_live_ts = last_live_ts.replace(tzinfo=timezone.utc)
    age = now - last_live_ts
    age_hours = age.total_seconds() / 3600.0
    return (age_hours > DROUGHT_THRESHOLD_HOURS, age_hours)


async def check_live_drought(
    pool,
    notifier,
    *,
    trading_mode: str,
    trading_paused: str,
    now: Optional[datetime] = None,
) -> None:
    """Run the drought check and post to Discord if it fires.

    Honors a 12h cooldown via ``bot_state`` so sustained droughts don't
    spam the channel. Restart-safe.
    """
    now = now or _now()
    inputs = await _query_drought_inputs(pool)
    if "error" in inputs:
        return
    fires, age_hours = _drought_should_fire(
        last_live_ts=inputs["last_live_ts"],
        paper_count_36h=inputs["paper_count_36h"],
        trading_mode=trading_mode,
        trading_paused=trading_paused,
        now=now,
    )
    if not fires:
        return
    state = await _get_bot_state(pool, DROUGHT_BOT_STATE_KEY) or {}
    if _within_cooldown(state.get("last_fired"), DROUGHT_COOLDOWN_HOURS, now):
        return
    age_str = "never" if age_hours is None else f"{age_hours:.1f}h"
    logger.warning(
        "live_health.drought_alarm_firing",
        last_live_age_hours=age_hours,
        paper_trades_36h=inputs["paper_count_36h"],
    )
    if notifier is not None:
        await notifier.live_drought_alarm(
            last_live_age_str=age_str,
            paper_trades_36h=inputs["paper_count_36h"],
            threshold_hours=DROUGHT_THRESHOLD_HOURS,
        )
    await _save_bot_state(pool, DROUGHT_BOT_STATE_KEY, {
        "last_fired": now.isoformat(),
        "last_age_hours": age_hours,
        "last_paper_count": inputs["paper_count_36h"],
    })


# ─── 1b. EDGE skip-ratio ──────────────────────────────────────────────────

async def _query_skip_ratio_inputs(pool) -> dict:
    """Compute (rows_with_EDGE_skip_24h / total_rows_24h) plus top-5
    skip reasons over the same window.
    """
    try:
        async with pool.connection() as conn:
            row = await conn.execute(
                """SELECT
                       COUNT(*) FILTER (WHERE skip_reason LIKE 'EDGE%') AS edge,
                       COUNT(*) AS total
                   FROM signal_log
                   WHERE timestamp > NOW() - INTERVAL '24 hours'""",
            )
            counts_row = await row.fetchone()
            edge_count = int(counts_row[0]) if counts_row else 0
            total_count = int(counts_row[1]) if counts_row else 0

            row = await conn.execute(
                """SELECT skip_reason, COUNT(*) AS n
                   FROM signal_log
                   WHERE timestamp > NOW() - INTERVAL '24 hours'
                     AND skip_reason IS NOT NULL
                   GROUP BY skip_reason
                   ORDER BY n DESC
                   LIMIT 5""",
            )
            top_rows = await row.fetchall()
            top_reasons = [(r[0], int(r[1])) for r in top_rows]
    except Exception as e:
        logger.error("live_health.skip_ratio_query_failed", error=str(e))
        return {"edge_count": 0, "total_count": 0, "top_reasons": [], "error": str(e)}
    return {"edge_count": edge_count, "total_count": total_count,
            "top_reasons": top_reasons}


def _skip_ratio_should_fire(history: list[float]) -> tuple[bool, int]:
    """Pure decision: count consecutive trailing entries above threshold.

    Fires when the last ``SKIP_RATIO_REQUIRED_CONSECUTIVE`` entries are
    all > ``SKIP_RATIO_THRESHOLD``. Returns (fires, consecutive_count).
    """
    if len(history) < SKIP_RATIO_REQUIRED_CONSECUTIVE:
        return False, 0
    consecutive = 0
    for ratio in reversed(history):
        if ratio > SKIP_RATIO_THRESHOLD:
            consecutive += 1
        else:
            break
    return consecutive >= SKIP_RATIO_REQUIRED_CONSECUTIVE, consecutive


async def check_edge_skip_ratio(
    pool,
    notifier,
    *,
    trading_mode: str,
    now: Optional[datetime] = None,
) -> None:
    """Append today's ratio to bot_state history; fire if last 2+ are
    consecutive breaches.

    Only meaningful in live mode (paper trades aren't subject to the
    edge_profile filter), so we early-out for paper.
    """
    if trading_mode != "live":
        return
    now = now or _now()
    inputs = await _query_skip_ratio_inputs(pool)
    if "error" in inputs:
        return
    if inputs["total_count"] < 100:
        # Not enough signal log activity to draw any conclusion.
        return
    ratio = inputs["edge_count"] / inputs["total_count"]
    state = await _get_bot_state(pool, SKIP_RATIO_BOT_STATE_KEY) or {}
    history: list[float] = list(state.get("history", []))
    history.append(round(ratio, 4))
    history = history[-SKIP_RATIO_HISTORY_LIMIT:]
    fires, consecutive = _skip_ratio_should_fire(history)
    new_state: dict[str, Any] = {
        "history": history,
        "last_check": now.isoformat(),
    }
    if fires and not _within_cooldown(
        state.get("last_fired"), SKIP_RATIO_COOLDOWN_HOURS, now,
    ):
        logger.warning(
            "live_health.skip_ratio_alarm_firing",
            ratio=ratio, consecutive=consecutive,
            top_reasons=inputs["top_reasons"],
        )
        if notifier is not None:
            await notifier.edge_skip_ratio_alarm(
                ratio=ratio,
                consecutive=consecutive,
                top_reasons=inputs["top_reasons"],
            )
        new_state["last_fired"] = now.isoformat()
    else:
        new_state["last_fired"] = state.get("last_fired")
    await _save_bot_state(pool, SKIP_RATIO_BOT_STATE_KEY, new_state)


# ─── 1c. Direction imbalance ──────────────────────────────────────────────

async def _query_imbalance_inputs(pool) -> dict:
    """Counts of EDGE_SHORT_* and EDGE_LONG_*-style rejections plus live
    short trades over a 7-day window.

    A rejection is "short" when the skip_reason contains 'SHORT' (e.g.
    EDGE_SHORT_BLOCKED, EDGE_SHORT_PRICE_LOW_*); "long" when it contains
    'LONG' or starts with EDGE_PRICE_CAP (long-side cap). Drivers and
    hour blocks are intentionally NOT classified as a side because they
    apply equally to both directions in the resolver pipeline.
    """
    try:
        async with pool.connection() as conn:
            row = await conn.execute(
                """SELECT
                       COUNT(*) FILTER (
                           WHERE skip_reason LIKE 'EDGE%'
                             AND (skip_reason LIKE '%SHORT%'
                                  OR skip_reason LIKE '%_NO_%')
                       ) AS short_rejected,
                       COUNT(*) FILTER (
                           WHERE skip_reason LIKE 'EDGE%'
                             AND (skip_reason LIKE '%LONG%'
                                  OR skip_reason LIKE 'EDGE_PRICE_CAP%')
                       ) AS long_rejected
                   FROM signal_log
                   WHERE timestamp > NOW() - INTERVAL '7 days'""",
            )
            counts = await row.fetchone()
            short_rejected = int(counts[0]) if counts else 0
            long_rejected = int(counts[1]) if counts else 0

            row = await conn.execute(
                """SELECT COUNT(*) FROM trades
                   WHERE trading_mode = 'live'
                     AND direction = 'short'
                     AND timestamp > NOW() - INTERVAL '7 days'""",
            )
            live_shorts_row = await row.fetchone()
            live_short_count = int(live_shorts_row[0]) if live_shorts_row else 0
    except Exception as e:
        logger.error("live_health.imbalance_query_failed", error=str(e))
        return {"short_rejected": 0, "long_rejected": 0,
                "live_short_count": 0, "error": str(e)}
    return {"short_rejected": short_rejected, "long_rejected": long_rejected,
            "live_short_count": live_short_count}


def _imbalance_should_fire(
    *,
    short_rejected: int,
    long_rejected: int,
    live_short_count: int,
) -> bool:
    """Pure decision. Fires when shorts rejections vastly outweigh longs
    AND no shorts have actually traded live recently."""
    if live_short_count > 0:
        return False
    if short_rejected < IMBALANCE_MIN_SHORT_REJECTIONS:
        return False
    # Avoid div-by-zero — if longs were rejected zero times, any short
    # rejections at all qualify (above the min-threshold gate above).
    if long_rejected == 0:
        return True
    return short_rejected > IMBALANCE_RATIO * long_rejected


async def check_direction_imbalance(
    pool,
    notifier,
    *,
    trading_mode: str,
    now: Optional[datetime] = None,
) -> None:
    """Direction-skip imbalance alarm with a 24h cooldown."""
    if trading_mode != "live":
        return
    now = now or _now()
    inputs = await _query_imbalance_inputs(pool)
    if "error" in inputs:
        return
    if not _imbalance_should_fire(
        short_rejected=inputs["short_rejected"],
        long_rejected=inputs["long_rejected"],
        live_short_count=inputs["live_short_count"],
    ):
        return
    state = await _get_bot_state(pool, IMBALANCE_BOT_STATE_KEY) or {}
    if _within_cooldown(state.get("last_fired"), IMBALANCE_COOLDOWN_HOURS, now):
        return
    logger.warning(
        "live_health.imbalance_alarm_firing",
        short_rejected=inputs["short_rejected"],
        long_rejected=inputs["long_rejected"],
        live_short_count=inputs["live_short_count"],
    )
    if notifier is not None:
        await notifier.direction_imbalance_alarm(
            short_rejected=inputs["short_rejected"],
            long_rejected=inputs["long_rejected"],
            live_short_count_7d=inputs["live_short_count"],
        )
    await _save_bot_state(pool, IMBALANCE_BOT_STATE_KEY, {
        "last_fired": now.isoformat(),
        "last_short_rejected": inputs["short_rejected"],
        "last_long_rejected": inputs["long_rejected"],
    })


# ─── Diagnostics: snapshot of the alarm conditions ────────────────────────

async def fetch_edge_profile_health(pool) -> dict:
    """Read-only snapshot of every signal the alarms watch. Surfaced on
    GET /api/diagnostics and GET /api/status so the dashboard can show
    a card without anyone having to wait for an alarm to fire.

    All errors degrade gracefully — missing fields default to None so a
    DB blip doesn't 500 the whole status endpoint.
    """
    out: dict[str, Any] = {
        "last_live_trade_age_hours": None,
        "paper_trades_36h": 0,
        "edge_skip_ratio_24h": None,
        "top_skip_reasons_24h": [],
        "edge_short_rejected_7d": 0,
        "edge_long_rejected_7d": 0,
        "edge_short_negative_roc_skips_24h": 0,
        "live_short_count_7d": 0,
        "auto_apply_enabled": False,
        "recent_auto_changes": [],
    }
    try:
        from config import settings as _settings
        out["auto_apply_enabled"] = bool(
            getattr(_settings.edge_profile, "auto_apply_enabled", False)
        )
    except Exception:
        pass

    try:
        async with pool.connection() as conn:
            row = await conn.execute(
                """SELECT MAX(timestamp) FROM trades WHERE trading_mode = 'live'""",
            )
            r = await row.fetchone()
            last_ts = r[0] if r else None
            if last_ts is not None:
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
                out["last_live_trade_age_hours"] = round(
                    (_now() - last_ts).total_seconds() / 3600.0, 2,
                )

            row = await conn.execute(
                """SELECT COUNT(*) FROM trades
                   WHERE trading_mode = 'paper'
                     AND timestamp > NOW() - INTERVAL '36 hours'""",
            )
            r = await row.fetchone()
            out["paper_trades_36h"] = int(r[0]) if r else 0

            row = await conn.execute(
                """SELECT
                       COUNT(*) FILTER (WHERE skip_reason LIKE 'EDGE%') AS edge,
                       COUNT(*) AS total
                   FROM signal_log
                   WHERE timestamp > NOW() - INTERVAL '24 hours'""",
            )
            r = await row.fetchone()
            edge = int(r[0]) if r else 0
            total = int(r[1]) if r else 0
            out["edge_skip_ratio_24h"] = (
                round(edge / total, 4) if total > 0 else None
            )

            row = await conn.execute(
                """SELECT skip_reason, COUNT(*) AS n
                   FROM signal_log
                   WHERE timestamp > NOW() - INTERVAL '24 hours'
                     AND skip_reason IS NOT NULL
                   GROUP BY skip_reason
                   ORDER BY n DESC
                   LIMIT 5""",
            )
            rows = await row.fetchall()
            out["top_skip_reasons_24h"] = [
                {"reason": r[0], "count": int(r[1])} for r in rows
            ]

            row = await conn.execute(
                """SELECT
                       COUNT(*) FILTER (
                           WHERE skip_reason LIKE 'EDGE%'
                             AND skip_reason LIKE '%SHORT%'
                       ),
                       COUNT(*) FILTER (
                           WHERE skip_reason LIKE 'EDGE%'
                             AND (skip_reason LIKE '%LONG%'
                                  OR skip_reason LIKE 'EDGE_PRICE_CAP%')
                       )
                   FROM signal_log
                   WHERE timestamp > NOW() - INTERVAL '7 days'""",
            )
            r = await row.fetchone()
            out["edge_short_rejected_7d"] = int(r[0]) if r else 0
            out["edge_long_rejected_7d"] = int(r[1]) if r else 0

            row = await conn.execute(
                """SELECT COUNT(*) FROM trades
                   WHERE trading_mode = 'live' AND direction = 'short'
                     AND timestamp > NOW() - INTERVAL '7 days'""",
            )
            r = await row.fetchone()
            out["live_short_count_7d"] = int(r[0]) if r else 0

            # New 2026-05-02: surface ROC-contradiction veto activity so
            # the operator can see it without grepping signal_log. Two
            # consecutive zeros over 24h with active live trading means
            # the gate isn't firing for any of the right reasons; a
            # spike (>500/24h) means the market regime has shifted to
            # one this gate considers persistently hostile to shorts.
            row = await conn.execute(
                """SELECT COUNT(*) FROM signal_log
                   WHERE timestamp > NOW() - INTERVAL '24 hours'
                     AND skip_reason LIKE 'EDGE_SHORT_NEGATIVE_ROC_%'""",
            )
            r = await row.fetchone()
            out["edge_short_negative_roc_skips_24h"] = int(r[0]) if r else 0

            try:
                row = await conn.execute(
                    """SELECT changed_at, param, old_value, new_value, applied_by
                       FROM edge_profile_change_log
                       ORDER BY changed_at DESC LIMIT 5""",
                )
                rows = await row.fetchall()
                out["recent_auto_changes"] = [
                    {
                        "changed_at": (
                            r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0])
                        ),
                        "param": r[1],
                        "old_value": r[2],
                        "new_value": r[3],
                        "applied_by": r[4],
                    }
                    for r in rows
                ]
            except Exception:
                # Table may not exist yet (migration 006 not run). Default
                # already initialised to [], so silently move on.
                pass
    except Exception as e:
        logger.warning("live_health.fetch_health_snapshot_failed", error=str(e))

    return out


# ─── Top-level scheduler entry ────────────────────────────────────────────

async def run_live_health_checks(
    pool,
    notifier,
    *,
    trading_mode: str,
    trading_paused: str,
    now: Optional[datetime] = None,
) -> None:
    """Run all three checks back-to-back. Each handles its own errors so
    a failure in one doesn't block the others."""
    now = now or _now()
    await check_live_drought(
        pool, notifier,
        trading_mode=trading_mode, trading_paused=trading_paused, now=now,
    )
    await check_edge_skip_ratio(
        pool, notifier, trading_mode=trading_mode, now=now,
    )
    await check_direction_imbalance(
        pool, notifier, trading_mode=trading_mode, now=now,
    )
