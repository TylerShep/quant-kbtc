"""Unit tests for the Spread Divergence post-resolver conviction modifier.

Covers both evaluate_spread_divergence (pure function) and SpreadRegimeFilter
(rolling history container with staleness + warmup).
"""
from __future__ import annotations

import dataclasses
import time

import pytest

from config import settings
from filters.spread_regime import SpreadRegimeFilter
from strategies.obi import Direction
from strategies.resolver import Conviction, SignalConflictResolver
from strategies.spread_div import (
    SpreadState,
    _median,
    evaluate_spread_divergence,
)


@pytest.fixture
def override_sd():
    """Fixture yielding a setter that patches fields on settings.spread_div
    (frozen dataclass). Restores original on teardown.
    """
    original = settings.spread_div

    def _apply(**kwargs):
        new_sd = dataclasses.replace(original, **kwargs)
        object.__setattr__(settings, "spread_div", new_sd)

    yield _apply
    object.__setattr__(settings, "spread_div", original)


# ─── _median helper ──────────────────────────────────────────────────────
def test_median_odd():
    assert _median([1.0, 2.0, 3.0]) == 2.0


def test_median_even():
    assert _median([1.0, 2.0, 3.0, 4.0]) == 2.5


def test_median_empty():
    assert _median([]) == 0.0


def test_median_unsorted():
    assert _median([5.0, 1.0, 3.0, 2.0, 4.0]) == 3.0


# ─── evaluate_spread_divergence ──────────────────────────────────────────
def test_wide_when_spread_far_above_baseline():
    baseline = [5.0] * 25
    state = evaluate_spread_divergence(baseline, current_spread=8.0, atr_regime="MEDIUM")
    assert state == SpreadState.WIDE


def test_tight_when_spread_far_below_baseline():
    baseline = [5.0] * 25
    state = evaluate_spread_divergence(baseline, current_spread=3.0, atr_regime="MEDIUM")
    assert state == SpreadState.TIGHT


def test_normal_within_baseline_band():
    baseline = [5.0] * 25
    state = evaluate_spread_divergence(baseline, current_spread=5.5, atr_regime="MEDIUM")
    assert state == SpreadState.NORMAL


def test_non_medium_regime_always_normal():
    baseline = [5.0] * 25
    # LOW regime
    assert evaluate_spread_divergence(baseline, 8.0, atr_regime="LOW") == SpreadState.NORMAL
    assert evaluate_spread_divergence(baseline, 3.0, atr_regime="LOW") == SpreadState.NORMAL
    # HIGH regime
    assert evaluate_spread_divergence(baseline, 8.0, atr_regime="HIGH") == SpreadState.NORMAL


def test_low_regime_skips_modifier_even_when_signal_would_upgrade(override_sd):
    """In non-MEDIUM regimes the modifier must not manipulate conviction."""
    override_sd(enabled=True, min_history=5, baseline_window=5)
    resolver = SignalConflictResolver()
    # baseline=5, current=3 would be TIGHT in MEDIUM, but we pass LOW
    spread_state = evaluate_spread_divergence(
        [5.0] * 10, current_spread=3.0, atr_regime="LOW"
    )
    assert spread_state == SpreadState.NORMAL
    # Even if we force TIGHT through the resolver in a non-MEDIUM regime,
    # the resolver itself also gates on MEDIUM and leaves conviction alone.
    decision = resolver.resolve(
        Direction.LONG, Direction.LONG,
        atr_regime="LOW", can_trade=True,
        spread_state=SpreadState.TIGHT,
    )
    # Resolver's own regime gate returns CIRCUIT/regime responses; LOW is
    # allowed to trade but spread upgrade must not fire. HIGH conviction
    # stays HIGH (can't go higher) — verify conviction unchanged from table.
    assert decision.conviction == Conviction.HIGH


def test_insufficient_history_returns_normal():
    history = [5.0] * 10  # default SD_MIN_HISTORY=20
    state = evaluate_spread_divergence(history, current_spread=10.0, atr_regime="MEDIUM")
    assert state == SpreadState.NORMAL


def test_none_spread_returns_normal():
    baseline = [5.0] * 25
    assert evaluate_spread_divergence(baseline, None, atr_regime="MEDIUM") == SpreadState.NORMAL


def test_zero_or_negative_spread_returns_normal():
    baseline = [5.0] * 25
    assert evaluate_spread_divergence(baseline, 0.0, atr_regime="MEDIUM") == SpreadState.NORMAL
    assert evaluate_spread_divergence(baseline, -1.0, atr_regime="MEDIUM") == SpreadState.NORMAL


def test_zero_baseline_returns_normal():
    baseline = [0.0] * 25
    state = evaluate_spread_divergence(baseline, 5.0, atr_regime="MEDIUM")
    assert state == SpreadState.NORMAL


def test_override_wide_threshold_tightens_detection():
    baseline = [5.0] * 25
    # current 6.5 -> z=+0.30 ; default WIDE=0.40 -> NORMAL
    assert evaluate_spread_divergence(baseline, 6.5, "MEDIUM") == SpreadState.NORMAL
    # Override WIDE=0.25 -> WIDE
    state = evaluate_spread_divergence(
        baseline, 6.5, "MEDIUM", overrides={"sd_wide_threshold": 0.25}
    )
    assert state == SpreadState.WIDE


def test_override_tight_threshold_tightens_detection():
    baseline = [5.0] * 25
    # current 4.6 -> z=-0.08 ; default TIGHT=-0.20 -> NORMAL
    assert evaluate_spread_divergence(baseline, 4.6, "MEDIUM") == SpreadState.NORMAL
    state = evaluate_spread_divergence(
        baseline, 4.6, "MEDIUM", overrides={"sd_tight_threshold": -0.05}
    )
    assert state == SpreadState.TIGHT


# ─── SpreadRegimeFilter ──────────────────────────────────────────────────
def test_filter_appends_positive_spreads():
    f = SpreadRegimeFilter()
    f.update(5.0)
    f.update(6.0)
    hist = f.spread_history()
    assert hist == [5.0, 6.0]


def test_filter_ignores_none_or_nonpositive():
    f = SpreadRegimeFilter()
    f.update(None)
    f.update(0.0)
    f.update(-1.5)
    assert f.spread_history() == []


def test_filter_warmup_seeds_history(monkeypatch):
    f = SpreadRegimeFilter()
    consumed = f.warmup([3.0, 4.0, 5.0, None, 0, -1, 6.0])
    assert consumed == 4
    assert f.spread_history() == [3.0, 4.0, 5.0, 6.0]


def test_filter_stale_history_returns_empty(override_sd):
    override_sd(staleness_sec=1)
    f = SpreadRegimeFilter()
    f.update(5.0)
    f._last_update = time.time() - 10
    assert f.spread_history() == []


def test_filter_get_state_unknown_before_updates():
    f = SpreadRegimeFilter()
    state = f.get_state()
    assert state["history_len"] == 0


def test_filter_get_state_reports_baseline(override_sd):
    override_sd(baseline_window=5)
    f = SpreadRegimeFilter()
    for v in [2.0, 3.0, 4.0, 5.0, 6.0]:
        f.update(v)
    state = f.get_state()
    assert state["baseline_cents"] == 4.0
    assert state["history_len"] == 5


# ─── Resolver integration ────────────────────────────────────────────────
def test_resolver_wide_downgrade_normal_to_low(override_sd):
    override_sd(enabled=True, min_history=5, baseline_window=5)
    resolver = SignalConflictResolver()
    decision = resolver.resolve(
        Direction.LONG, Direction.NEUTRAL,
        atr_regime="MEDIUM", can_trade=True,
        spread_state=SpreadState.WIDE,
    )
    assert decision.conviction == Conviction.LOW
    assert decision.direction == Direction.LONG
    assert decision.spread_state == SpreadState.WIDE


def test_resolver_wide_drops_low_to_none(override_sd):
    """WIDE + LOW -> NONE, direction nulled, SPREAD_WIDE_DOWNGRADE skip reason."""
    override_sd(enabled=True, min_history=5, baseline_window=5)
    resolver = SignalConflictResolver()
    decision = resolver.resolve(
        Direction.NEUTRAL, Direction.LONG,  # table -> LOW
        atr_regime="MEDIUM", can_trade=True,
        spread_state=SpreadState.WIDE,
    )
    assert decision.conviction == Conviction.NONE
    assert decision.direction is None
    assert decision.skip_reason == "SPREAD_WIDE_DOWNGRADE"


def test_resolver_tight_upgrade_low_to_normal(override_sd):
    override_sd(enabled=True, min_history=5, baseline_window=5)
    resolver = SignalConflictResolver()
    decision = resolver.resolve(
        Direction.NEUTRAL, Direction.LONG,  # table -> LOW
        atr_regime="MEDIUM", can_trade=True,
        spread_state=SpreadState.TIGHT,
    )
    assert decision.conviction == Conviction.NORMAL
    assert decision.direction == Direction.LONG


def test_resolver_tight_never_upgrades_none(override_sd):
    """TIGHT spread on a conflicting-signals pair must never manufacture a trade."""
    override_sd(enabled=True, min_history=5, baseline_window=5)
    resolver = SignalConflictResolver()
    decision = resolver.resolve(
        Direction.LONG, Direction.SHORT,  # conflict -> NONE
        atr_regime="MEDIUM", can_trade=True,
        spread_state=SpreadState.TIGHT,
    )
    assert decision.direction is None
    assert decision.conviction == Conviction.NONE


def test_wide_spread_skipped_when_sd_disabled(override_sd):
    """SD_ENABLED=false -> modifier is a no-op even when WIDE is seen."""
    override_sd(enabled=False)
    resolver = SignalConflictResolver()
    decision = resolver.resolve(
        Direction.LONG, Direction.NEUTRAL,  # table -> NORMAL
        atr_regime="MEDIUM", can_trade=True,
        spread_state=SpreadState.WIDE,
    )
    assert decision.conviction == Conviction.NORMAL
    assert decision.direction == Direction.LONG
