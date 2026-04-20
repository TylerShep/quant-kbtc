"""Unit tests for the ML entry gate.

Covers the contract that the coordinator depends on:
  * No model loaded -> passthrough (allowed=True)
  * Model loaded, p_win >= threshold -> allowed
  * Model loaded, p_win <  threshold -> blocked
  * Inference exception -> fail-open (allowed=True)

These tests do not touch disk or train a real model. They monkeypatch the
module-level ``_artifact`` with a tiny stub so behavior is deterministic.
"""
from __future__ import annotations

import pytest

from ml import inference


class _StubModel:
    """Stand-in for the XGBoost classifier — controllable p_win for tests."""

    def __init__(self, p_win: float):
        self._p_win = p_win

    def predict_proba(self, _row):
        return [[1.0 - self._p_win, self._p_win]]


class _ErrorModel:
    def predict_proba(self, _row):
        raise RuntimeError("boom")


@pytest.fixture
def reset_artifact():
    """Restore _artifact after each test so cases don't leak state."""
    saved = inference._artifact
    yield
    inference._artifact = saved


def _set_artifact(p_win: float = 0.7, threshold: float = 0.55,
                  features=("obi", "roc_3")):
    inference._artifact = {
        "model": _StubModel(p_win),
        "features": list(features),
        "threshold": threshold,
    }


def test_no_model_loaded_passthrough(reset_artifact):
    """When the .pkl is missing, ml_gate must behave as if absent."""
    inference._artifact = None
    allowed, p_win = inference.ml_gate({"obi": 0.7, "roc_3": 0.5})
    assert allowed is True
    assert p_win == pytest.approx(0.5)


def test_allows_when_above_threshold(reset_artifact):
    _set_artifact(p_win=0.70, threshold=0.55)
    allowed, p_win = inference.ml_gate({"obi": 0.7, "roc_3": 0.5})
    assert allowed is True
    assert p_win == pytest.approx(0.70)


def test_blocks_when_below_threshold(reset_artifact):
    _set_artifact(p_win=0.40, threshold=0.55)
    allowed, p_win = inference.ml_gate({"obi": 0.2, "roc_3": -0.4})
    assert allowed is False
    assert p_win == pytest.approx(0.40)


def test_boundary_at_threshold_is_allowed(reset_artifact):
    """p_win == threshold should be admitted (inference uses >=)."""
    _set_artifact(p_win=0.55, threshold=0.55)
    allowed, p_win = inference.ml_gate({"obi": 0.5, "roc_3": 0.0})
    assert allowed is True
    assert p_win == pytest.approx(0.55)


def test_missing_feature_treated_as_zero(reset_artifact):
    """Missing keys in the input dict should not crash; default is 0."""
    _set_artifact(p_win=0.6, threshold=0.55, features=("obi", "roc_3", "atr_pct"))
    allowed, p_win = inference.ml_gate({"obi": 0.7})
    assert allowed is True
    assert p_win == pytest.approx(0.6)


def test_none_feature_treated_as_zero(reset_artifact):
    """A None value in the input dict must not blow up np.array construction."""
    _set_artifact(p_win=0.6, threshold=0.55, features=("obi", "roc_3"))
    allowed, _ = inference.ml_gate({"obi": None, "roc_3": None})
    assert allowed is True


def test_inference_error_fails_open(reset_artifact):
    """If the model raises at predict time, gate must allow the trade and
    return passthrough sentinel (True, 0.5). The bot keeps trading even if
    a model artifact gets corrupted in production."""
    inference._artifact = {
        "model": _ErrorModel(),
        "features": ["obi"],
        "threshold": 0.55,
    }
    allowed, p_win = inference.ml_gate({"obi": 0.7})
    assert allowed is True
    assert p_win == pytest.approx(0.5)


def test_default_threshold_when_missing(reset_artifact):
    """Artifacts without a `threshold` key default to 0.55."""
    inference._artifact = {
        "model": _StubModel(0.54),
        "features": ["obi"],
    }
    allowed, _ = inference.ml_gate({"obi": 0.5})
    assert allowed is False  # 0.54 < default 0.55

    inference._artifact["model"] = _StubModel(0.55)
    allowed, _ = inference.ml_gate({"obi": 0.5})
    assert allowed is True


def test_load_model_no_file_is_safe(reset_artifact, tmp_path, monkeypatch):
    """load_model() must not raise when the model file is missing."""
    monkeypatch.setattr(inference, "_MODEL_PATH", tmp_path / "does_not_exist.pkl")
    inference._artifact = "sentinel-not-touched"
    inference.load_model()
    # Loader noticed the missing file and bailed without overwriting state.
    # The contract is "fail open at gate time", so we only assert no exception
    # and that a subsequent gate call still works (passthrough since no model).
    inference._artifact = None
    allowed, p_win = inference.ml_gate({})
    assert allowed is True
    assert p_win == pytest.approx(0.5)


def test_ml_config_defaults_off():
    """The gate must ship disabled. Operator must explicitly opt in via env."""
    from config.settings import MLConfig
    cfg = MLConfig()
    assert cfg.gate_enabled is False, (
        "ML_GATE_ENABLED must default to False so deploys don't accidentally "
        "activate an untrained gate."
    )
    assert cfg.gate_paper is True, "Paper lane is the default activation target."
    assert cfg.gate_live is False, "Live lane stays off until paper validates."
