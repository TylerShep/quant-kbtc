"""Unit tests for ATRRegimeFilter."""
from types import SimpleNamespace
from unittest.mock import patch

from config import settings
from filters.atr_regime import ATRRegimeFilter

def test_starts_at_medium():
    f = ATRRegimeFilter()
    assert f.current_regime == "MEDIUM"


def test_transitions_to_high_with_large_true_ranges():
    f = ATRRegimeFilter()
    f.update(100.0, 100.0, 100.0)
    period = settings.atr.period
    smooth = settings.atr.smooth_period
    confirm = settings.atr.regime_confirm_bars
    iterations = period + smooth + confirm + 5
    for _ in range(iterations):
        f.update(200.0, 0.0, 100.0)
    assert f.current_regime == "HIGH"


def test_transitions_to_low_with_small_true_ranges():
    f = ATRRegimeFilter()
    f.update(100.0, 100.0, 100.0)
    period = settings.atr.period
    smooth = settings.atr.smooth_period
    confirm = settings.atr.regime_confirm_bars
    iterations = period + smooth + confirm + 5
    for _ in range(iterations):
        f.update(100.01, 99.99, 100.0)
    assert f.current_regime == "LOW"


def test_confirmation_bars_required_before_regime_change():
    """Regime commits only when all bars in the confirmation deque agree on raw classification."""
    fake_atr = SimpleNamespace(
        period=1,
        low_threshold=0.0,
        high_threshold=1.0,
        smooth_period=1,
        regime_confirm_bars=3,
    )
    fake_settings = SimpleNamespace(atr=fake_atr)
    seq = iter(["MEDIUM", "MEDIUM", "HIGH", "HIGH", "HIGH"])

    def fake_classify(self, atr_pct: float) -> str:
        return next(seq)

    with patch("filters.atr_regime.settings", fake_settings):
        with patch.object(ATRRegimeFilter, "_classify", fake_classify):
            f = ATRRegimeFilter()
            f.update(100.0, 100.0, 100.0)
            f.update(110.0, 90.0, 100.0)
            assert f.current_regime == "MEDIUM"
            f.update(110.0, 90.0, 100.0)
            assert f.current_regime == "MEDIUM"
            f.update(110.0, 90.0, 100.0)
            assert f.current_regime == "MEDIUM"
            f.update(110.0, 90.0, 100.0)
            assert f.current_regime == "MEDIUM"
            f.update(110.0, 90.0, 100.0)
            assert f.current_regime == "HIGH"
