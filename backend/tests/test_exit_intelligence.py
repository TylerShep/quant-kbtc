"""Unit tests for exit-intelligence scoring helpers."""
from __future__ import annotations

from strategies.exit_intelligence import (
    compute_position_health_score,
    momentum_decay_component,
)


def test_health_score_high_when_trade_thesis_is_intact():
    score, components = compute_position_health_score(
        direction="long",
        current_obi=0.78,
        current_roc=0.95,
        entry_roc=0.60,
        atr_regime="MEDIUM",
        regime_at_entry="MEDIUM",
        pnl_pct=0.026,
        max_favorable_excursion=0.030,
        mini_roc_fast=0.30,
        mini_roc_slow=0.25,
        weight_obi=0.30,
        weight_roc=0.20,
        weight_regime=0.15,
        weight_mfe=0.20,
        weight_momentum=0.15,
    )
    assert score >= 75.0
    assert components.obi_alignment > 0.9
    assert components.mfe_retention > 0.8
    assert components.momentum_decay > 0.9


def test_health_score_low_when_signal_decay_and_giveback_hit():
    score, components = compute_position_health_score(
        direction="long",
        current_obi=0.42,
        current_roc=-0.45,
        entry_roc=0.80,
        atr_regime="HIGH",
        regime_at_entry="LOW",
        pnl_pct=-0.004,
        max_favorable_excursion=0.018,
        mini_roc_fast=-0.12,
        mini_roc_slow=0.20,
        weight_obi=0.30,
        weight_roc=0.20,
        weight_regime=0.15,
        weight_mfe=0.20,
        weight_momentum=0.15,
    )
    assert score <= 25.0
    assert components.roc_alignment == 0.0
    assert components.atr_regime_stability == 0.0
    assert components.momentum_decay <= 0.2


def test_momentum_decay_component_detects_deceleration():
    # Both windows still positive, but fast momentum collapsed vs slow window.
    val = momentum_decay_component("long", mini_roc_fast=0.04, mini_roc_slow=0.25)
    assert val <= 0.2


def test_momentum_decay_component_is_direction_aware_for_shorts():
    # Negative ROC is favorable for short direction, so this should score healthy.
    val = momentum_decay_component("short", mini_roc_fast=-0.30, mini_roc_slow=-0.24)
    assert val >= 0.75

