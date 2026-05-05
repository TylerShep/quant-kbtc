"""Exit-intelligence helpers for health-score based trade exits."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


@dataclass
class HealthComponents:
    """Per-signal sub-scores used by the aggregate health score."""

    obi_alignment: float
    roc_alignment: float
    atr_regime_stability: float
    mfe_retention: float
    momentum_decay: float

    def to_dict(self) -> dict:
        return {
            "obi_alignment": round(self.obi_alignment, 4),
            "roc_alignment": round(self.roc_alignment, 4),
            "atr_regime_stability": round(self.atr_regime_stability, 4),
            "mfe_retention": round(self.mfe_retention, 4),
            "momentum_decay": round(self.momentum_decay, 4),
        }


def _obi_alignment_score(direction: str, current_obi: Optional[float]) -> float:
    if current_obi is None:
        return 0.5
    if direction == "long":
        return _clamp((current_obi - 0.5) / 0.25)
    return _clamp((0.5 - current_obi) / 0.25)


def _roc_alignment_score(
    direction: str,
    current_roc: Optional[float],
    entry_roc: Optional[float],
) -> float:
    if current_roc is None:
        return 0.5

    directional_sign = 1 if direction == "long" else -1
    entry_sign = 0
    if entry_roc is not None:
        if entry_roc > 0:
            entry_sign = 1
        elif entry_roc < 0:
            entry_sign = -1
    if entry_sign == 0:
        entry_sign = directional_sign

    current_sign = 0
    if current_roc > 0:
        current_sign = 1
    elif current_roc < 0:
        current_sign = -1

    if current_sign == 0:
        return 0.4
    if current_sign != entry_sign:
        return 0.0

    baseline = max(abs(entry_roc or 0.0), 0.01)
    strength = abs(current_roc) / baseline
    if strength >= 1.0:
        return 1.0
    if strength >= 0.6:
        return 0.75
    if strength >= 0.3:
        return 0.5
    return 0.35


def _atr_regime_stability_score(
    atr_regime: Optional[str], regime_at_entry: Optional[str]
) -> float:
    if atr_regime is None:
        return 0.5
    if atr_regime == "HIGH":
        return 0.0
    if regime_at_entry and atr_regime == regime_at_entry:
        return 1.0
    if atr_regime == "MEDIUM":
        return 0.75
    if atr_regime == "LOW":
        return 0.65
    return 0.5


def _mfe_retention_score(
    pnl_pct: Optional[float],
    max_favorable_excursion: Optional[float],
) -> float:
    if pnl_pct is None:
        return 0.5
    if max_favorable_excursion is None:
        return 0.5
    if max_favorable_excursion <= 0:
        return 0.25 if pnl_pct < 0 else 0.6

    giveback = max(0.0, max_favorable_excursion - pnl_pct)
    retention = 1.0 - _clamp(giveback / max(max_favorable_excursion, 1e-6))
    if pnl_pct < 0:
        retention *= 0.5
    return _clamp(retention)


def momentum_decay_component(
    direction: str,
    mini_roc_fast: Optional[float],
    mini_roc_slow: Optional[float],
) -> float:
    """Score micro-momentum health (1.0 healthy, 0.0 heavily decayed)."""
    if mini_roc_fast is None and mini_roc_slow is None:
        return 0.5

    fast = mini_roc_fast if mini_roc_fast is not None else mini_roc_slow
    slow = mini_roc_slow if mini_roc_slow is not None else mini_roc_fast
    if fast is None or slow is None:
        return 0.5

    # Normalize so positive values always mean "favorable for this direction".
    if direction == "short":
        fast = -fast
        slow = -slow

    if fast <= 0 and slow <= 0:
        return 0.0
    if fast > 0 and slow <= 0:
        return 0.65
    if fast <= 0 < slow:
        return 0.2

    ratio = fast / max(slow, 1e-6)
    if ratio >= 1.0:
        return 1.0
    if ratio >= 0.7:
        return 0.75
    if ratio >= 0.4:
        return 0.45
    return 0.2


def compute_position_health_score(
    *,
    direction: str,
    current_obi: Optional[float],
    current_roc: Optional[float],
    entry_roc: Optional[float],
    atr_regime: Optional[str],
    regime_at_entry: Optional[str],
    pnl_pct: Optional[float],
    max_favorable_excursion: Optional[float],
    mini_roc_fast: Optional[float],
    mini_roc_slow: Optional[float],
    weight_obi: float,
    weight_roc: float,
    weight_regime: float,
    weight_mfe: float,
    weight_momentum: float,
) -> tuple[float, HealthComponents]:
    """Return a 0-100 health score and its component breakdown."""
    components = HealthComponents(
        obi_alignment=_obi_alignment_score(direction, current_obi),
        roc_alignment=_roc_alignment_score(direction, current_roc, entry_roc),
        atr_regime_stability=_atr_regime_stability_score(atr_regime, regime_at_entry),
        mfe_retention=_mfe_retention_score(pnl_pct, max_favorable_excursion),
        momentum_decay=momentum_decay_component(direction, mini_roc_fast, mini_roc_slow),
    )

    total_weight = (
        max(weight_obi, 0.0)
        + max(weight_roc, 0.0)
        + max(weight_regime, 0.0)
        + max(weight_mfe, 0.0)
        + max(weight_momentum, 0.0)
    )
    if total_weight <= 0:
        return 50.0, components

    weighted = (
        components.obi_alignment * max(weight_obi, 0.0)
        + components.roc_alignment * max(weight_roc, 0.0)
        + components.atr_regime_stability * max(weight_regime, 0.0)
        + components.mfe_retention * max(weight_mfe, 0.0)
        + components.momentum_decay * max(weight_momentum, 0.0)
    )
    score = (weighted / total_weight) * 100.0
    return round(_clamp(score, 0.0, 100.0), 2), components

