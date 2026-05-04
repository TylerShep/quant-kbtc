"""
Live-lane edge profile filter.

Restricts the LIVE trading lane to the subset of trade setups that showed
statistically significant positive expectancy in paper trading.

Re-calibrated 2026-04-29 against 14-day paper window (2026-04-15 →
2026-04-29, 522 clean trades). Findings the new defaults encode:

  * Long side: 309 trades, +$114,197 net, 53% WR. Edge is robust across
    every UTC hour. Original 0-7 UTC block was costing +$36,313 of long
    PnL — hours 03/05/06 alone produced +$28,513. Block list cleared.
  * Short side: 213 trades, -$6,021 net overall, BUT cleanly bimodal:
      - Cheap (<$40) NORMAL conviction: 89 trades @ $30 bucket lost
        -$12,893 (37% WR). This is the entire short anti-edge.
      - HIGH conviction shorts: 12 trades, +$1,799 net, 75% WR.
      - Shorts at $50+: 56 trades, +$6,400 net, 70%+ WR.
    Replace ``EDGE_LIVE_LONG_ONLY=true`` blanket block with two targeted
    gates: ``short_min_price`` and ``short_min_conviction``.
  * Cheap entries (<$25) carry positive long expectancy still; the
    ``max_entry_price`` cap stays in place.
  * OBI+ROC agreement: 92.3% WR for longs, 72.7% WR for HIGH shorts.
    Stays exempt from the price cap.
  * OBI/TIGHT, ROC/TIGHT drivers and LOW conviction trades remain blocked
    per the prior calibrations (still negative or noisy in 14-day window).

Paper lane is NEVER affected by this filter — it continues to take the full
strategy for ongoing data collection and ML training.

The filter is a pure function of (decision, entry_price, now_utc, config),
so it is easy to test in isolation. It is the LAST filter in the entry
path before the order is sent.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple

import structlog

from config import settings
from strategies.obi import Direction
from strategies.resolver import Conviction, TradeDecision
from strategies.spread_div import SpreadState

logger = structlog.get_logger()


_CONVICTION_RANK = {
    Conviction.NONE: 0,
    Conviction.LOW: 1,
    Conviction.NORMAL: 2,
    Conviction.HIGH: 3,
}


def _driver_base(decision: TradeDecision) -> str:
    """Return the canonical driver label as it appears in allowed_drivers.

    Mirrors describe_signal_driver() but without /WIDE or /TIGHT suffix
    when we want to match by base. We DO include /TIGHT in the allowed
    list explicitly so it must match exactly — i.e. ROC/TIGHT is allowed,
    OBI/TIGHT is not.
    """
    return decision.signal_driver


def _meets_short_min_conviction(decision_conviction: Conviction, min_label: str) -> bool:
    """True when the decision's conviction is >= the configured minimum.

    Unknown labels fail closed (return False) to avoid silently bypassing
    the gate when an operator typos the env var.
    """
    try:
        min_conv = Conviction(min_label.upper())
    except ValueError:
        return False
    return _CONVICTION_RANK[decision_conviction] >= _CONVICTION_RANK[min_conv]


def evaluate(
    *,
    decision: TradeDecision,
    entry_price: Optional[float],
    now_utc: Optional[datetime] = None,
    roc_value: Optional[float] = None,
) -> Tuple[bool, Optional[str]]:
    """Return (allowed, skip_reason).

    Caller is responsible for invoking this only in live mode and only
    when ``settings.edge_profile.enabled`` is True. When ``enabled`` is
    False this function still works (returns allowed=True) but the
    coordinator should short-circuit before calling for zero overhead.

    Short-side gate (when ``long_only=False``):
      A short trade is allowed when EITHER ``entry_price >= short_min_price``
      OR ``conviction >= short_min_conviction``. Either gate alone is
      sufficient. The price branch is deferred until ``entry_price`` is
      known — a pre-filter call (``entry_price=None``) on a short with
      sub-threshold conviction passes through and gets re-evaluated once
      the coordinator looks up the live entry price.

    ROC-contradiction veto (NORMAL shorts only):
      When ``roc_value`` is provided and the trade is a NORMAL-conviction
      short with raw 5-bar ROC at or below
      ``short_block_negative_roc_threshold`` (default -0.05), reject it.
      See ``EdgeProfileConfig.short_block_negative_roc_threshold`` docstring
      for the calibration rationale. Backward-compatible:
      ``roc_value=None`` skips the gate (callers that haven't wired the
      raw ROC through pass through unchanged).
    """
    cfg = settings.edge_profile

    if not cfg.enabled:
        return True, None

    if decision.direction is None or decision.conviction == Conviction.NONE:
        return True, None

    if cfg.long_only and decision.direction != Direction.LONG:
        return False, "EDGE_SHORT_BLOCKED"

    if (
        not cfg.long_only
        and decision.direction == Direction.SHORT
    ):
        conv_ok = _meets_short_min_conviction(
            decision.conviction, cfg.short_min_conviction,
        )
        if not conv_ok:
            if entry_price is None:
                pass
            elif cfg.short_min_price > 0 and entry_price < cfg.short_min_price:
                return False, (
                    f"EDGE_SHORT_PRICE_LOW_{entry_price:.0f}c<{cfg.short_min_price:.0f}c"
                )

        # ROC-contradiction veto. Only fires for NORMAL conviction (HIGH
        # overrides). Threshold is negative; values <= threshold reject.
        # Set threshold to 0.0 (or positive) to disable the gate entirely.
        if (
            decision.conviction == Conviction.NORMAL
            and roc_value is not None
            and cfg.short_block_negative_roc_threshold < 0.0
            and roc_value <= cfg.short_block_negative_roc_threshold
        ):
            return False, (
                f"EDGE_SHORT_NEGATIVE_ROC_{roc_value:.3f}"
                f"<={cfg.short_block_negative_roc_threshold:.3f}"
            )

    if cfg.block_low_conviction and decision.conviction == Conviction.LOW:
        return False, "EDGE_LOW_CONVICTION_BLOCKED"

    driver = _driver_base(decision)
    allowed = cfg.allowed_drivers_set
    if allowed and driver not in allowed:
        return False, f"EDGE_DRIVER_BLOCKED_{driver}"

    blocked_hours = cfg.blocked_hours_set
    if blocked_hours:
        now = now_utc or datetime.now(timezone.utc)
        if now.hour in blocked_hours:
            return False, f"EDGE_HOUR_BLOCKED_{now.hour:02d}UTC"

    if entry_price is not None and cfg.max_entry_price > 0:
        is_agreement = (
            decision.obi_dir != Direction.NEUTRAL
            and decision.roc_dir != Direction.NEUTRAL
            and decision.obi_dir == decision.roc_dir
        )
        exempt = cfg.agreement_overrides_price_cap and is_agreement
        if (
            not exempt
            and decision.direction == Direction.LONG
            and entry_price > cfg.max_entry_price
        ):
            return False, f"EDGE_PRICE_CAP_{entry_price:.0f}c>{cfg.max_entry_price:.0f}c"

    return True, None
