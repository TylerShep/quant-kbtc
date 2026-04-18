"""
Spread Divergence modifier — post-resolver conviction adjuster.

Does NOT produce a directional signal. Computes whether the current
bid-ask spread is anomalously wide or tight relative to its rolling
baseline, and returns a SpreadState that the resolver uses to upgrade
or downgrade conviction.

Per the signal-conflict-resolver skill: adding a third directional
signal would require a 27-cell coordination table and make HIGH
conviction nearly unreachable. Instead, spread acts as a graduated
confidence modifier applied after the primary resolver decision.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from config import settings


class SpreadState(str, Enum):
    WIDE = "WIDE"      # spread anomalously wide -> downgrade conviction
    NORMAL = "NORMAL"  # spread within baseline range -> no adjustment
    TIGHT = "TIGHT"    # spread anomalously tight -> upgrade conviction


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2.0
    return s[mid]


def evaluate_spread_divergence(
    spread_history: list[float],
    current_spread: Optional[float],
    atr_regime: str,
    overrides: Optional[dict] = None,
) -> SpreadState:
    """Evaluate the spread divergence state.

    Args:
        spread_history: Rolling list of recent spread_cents readings.
        current_spread: Current spread_cents from FeatureSnapshot.
        atr_regime:     Current ATR regime string.
        overrides:      Optional dict for backtesting parameter sweeps.

    Returns:
        SpreadState.WIDE, NORMAL, or TIGHT.
    """
    cfg = settings.spread_div
    ov = overrides or {}

    if atr_regime != "MEDIUM":
        return SpreadState.NORMAL

    if current_spread is None or current_spread <= 0:
        return SpreadState.NORMAL

    min_history = ov.get("sd_min_history", cfg.min_history)
    if len(spread_history) < min_history:
        return SpreadState.NORMAL

    baseline_window = ov.get("sd_baseline_window", cfg.baseline_window)
    recent = spread_history[-baseline_window:]
    baseline = _median(recent)

    if baseline <= 0:
        return SpreadState.NORMAL

    spread_z = (current_spread - baseline) / baseline

    wide_thresh = ov.get("sd_wide_threshold", cfg.wide_threshold)
    tight_thresh = ov.get("sd_tight_threshold", cfg.tight_threshold)

    if spread_z >= wide_thresh:
        return SpreadState.WIDE
    if spread_z <= tight_thresh:
        return SpreadState.TIGHT
    return SpreadState.NORMAL
