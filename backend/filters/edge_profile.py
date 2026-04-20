"""
Live-lane edge profile filter.

Restricts the LIVE trading lane to the subset of trade setups that showed
statistically significant positive expectancy in 7 days of paper trading
(2026-04-13 → 2026-04-19, 245 clean trades after the bankroll-sizing fix).

Findings the defaults encode:
  * Long side: t=3.31 significant (+$33.13/trade avg over 148 trades)
  * Short side: t=-2.17 significant (-$15.17/trade avg over 97 trades) → block
  * Entry price ≤ $25: 59.3% WR vs 48% baseline → cap
  * OBI+ROC agreement: 88.9% WR (8/9, p≈0.020) → exempt from price cap
  * OBI/TIGHT driver: net -$329 across 10 trades → block
  * ROC LOW: profit dominated by single $1 settlement windfall → block
  * Asia overnight (0-7 UTC): +$1.83/trade, near-zero edge → block

Paper lane is NEVER affected by this filter — it continues to take the full
strategy for ongoing data collection and ML model training.

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


def _driver_base(decision: TradeDecision) -> str:
    """Return the canonical driver label as it appears in allowed_drivers.

    Mirrors describe_signal_driver() but without /WIDE or /TIGHT suffix
    when we want to match by base. We DO include /TIGHT in the allowed
    list explicitly so it must match exactly — i.e. ROC/TIGHT is allowed,
    OBI/TIGHT is not.
    """
    return decision.signal_driver


def evaluate(
    *,
    decision: TradeDecision,
    entry_price: Optional[float],
    now_utc: Optional[datetime] = None,
) -> Tuple[bool, Optional[str]]:
    """Return (allowed, skip_reason).

    Caller is responsible for invoking this only in live mode and only
    when ``settings.edge_profile.enabled`` is True. When ``enabled`` is
    False this function still works (returns allowed=True) but the
    coordinator should short-circuit before calling for zero overhead.
    """
    cfg = settings.edge_profile

    if not cfg.enabled:
        return True, None

    if decision.direction is None or decision.conviction == Conviction.NONE:
        # Nothing to filter — there's no trade.
        return True, None

    if cfg.long_only and decision.direction != Direction.LONG:
        return False, "EDGE_SHORT_BLOCKED"

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
        if not exempt and entry_price > cfg.max_entry_price:
            return False, f"EDGE_PRICE_CAP_{entry_price:.0f}c>{cfg.max_entry_price:.0f}c"

    return True, None
