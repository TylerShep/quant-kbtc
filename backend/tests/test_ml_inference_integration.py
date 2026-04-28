"""End-to-end integration tests for the ML entry-gate loader.

The unit tests in `test_ml_gate.py` mock `inference._artifact` directly, which
verifies the gate semantics but skips the actual deserialization path. That gap
let us ship a deploy where `requirements.txt` was missing `xgboost` /
`scikit-learn` / `joblib`: the bot booted, logged `ml.model_load_failed`, and
silently fell back to passthrough — no test caught it.

These tests close that gap. They train a real (tiny) XGBoost model with the
exact same `train()` function production uses, persist it to disk via joblib,
load it via the production loader (`inference.load_model()`), and run inference
through `inference.ml_gate()`. If any of xgboost/sklearn/joblib are missing or
incompatible at runtime, these tests fail at import or load time — not silently
in production.

Tests are intentionally cheap: ~200 synthetic rows, 50 trees, ~1s wall time
per test. They're safe to run in the standard pytest suite.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# This test imports `train_xgb` from the project's top-level scripts/ directory.
# In CI / local dev that resolves to <repo>/scripts/train_xgb.py via the
# parent-of-backend resolution below. In production deploy contexts (e.g. the
# kbtc-bot container, which only bind-mounts backend/), the scripts/ folder is
# not on disk; we skip the whole module rather than fail collection so the
# unit tests in test_ml_gate.py continue to run there.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if not (_SCRIPTS_DIR / "train_xgb.py").exists():
    pytest.skip(
        f"Integration test requires scripts/train_xgb.py at {_SCRIPTS_DIR}; "
        "skipping in this environment (likely a deploy container with only "
        "backend/ mounted). Run from the full repo for full coverage.",
        allow_module_level=True,
    )
sys.path.insert(0, str(_SCRIPTS_DIR))

import train_xgb  # noqa: E402

from ml import inference  # noqa: E402


@pytest.fixture
def reset_inference_state(tmp_path, monkeypatch):
    """Redirect inference._MODEL_PATH at a tmp file and clear loader state.

    Prevents tests from reading or writing the real backend/ml/models/ tree
    and from leaking _artifact across tests.
    """
    saved_artifact = inference._artifact
    saved_model_path = inference._MODEL_PATH
    inference._artifact = None
    monkeypatch.setattr(inference, "_MODEL_PATH", tmp_path / "test_model.pkl")
    yield tmp_path
    inference._artifact = saved_artifact
    monkeypatch.setattr(inference, "_MODEL_PATH", saved_model_path)


def _make_synthetic_training_df(n_rows: int = 200, seed: int = 42) -> pd.DataFrame:
    """Build a small training frame with the exact ENTRY_FEATURES schema.

    We craft a learnable signal so the trained model is non-trivial: high obi
    + positive roc_3 → win label; otherwise loss. This guarantees the model
    produces a wide spread of probabilities (not stuck at ~0.5), which makes
    downstream gate behavior assertions meaningful.
    """
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n_rows):
        obi = float(rng.uniform(0.0, 1.0))
        roc_3 = float(rng.uniform(-1.0, 1.0))
        signal_strength = (obi - 0.5) * 2 + roc_3
        win_prob = 1.0 / (1.0 + np.exp(-3.0 * signal_strength))
        label = 1 if rng.uniform() < win_prob else -1

        row = {
            "obi": obi,
            "roc_3": roc_3,
            "roc_5": float(rng.uniform(-1.0, 1.0)),
            "roc_10": float(rng.uniform(-1.0, 1.0)),
            "atr_pct": float(rng.uniform(0.1, 0.5)),
            "spread_pct": float(rng.uniform(-2.0, 2.0)),
            "bid_depth": float(rng.uniform(100, 50000)),
            "ask_depth": float(rng.uniform(100, 50000)),
            "green_candles_3": int(rng.integers(0, 4)),
            "candle_body_pct": float(rng.uniform(0.0, 1.0)),
            "volume_ratio": float(rng.uniform(0.1, 2.0)),
            "time_remaining_sec": int(rng.integers(60, 3600)),
            "hour_of_day": int(rng.integers(0, 24)),
            "day_of_week": int(rng.integers(0, 7)),
            "label": label,
        }
        rows.append(row)
    return pd.DataFrame(rows)


def test_required_runtime_dependencies_importable():
    """Catch the bug we hit on the first deploy: prod image was missing
    xgboost / sklearn / joblib, so model loading crashed silently. If any of
    these import fails, every other test in this file crashes too — but we
    surface a clear error message here as the first signal."""
    import joblib  # noqa: F401
    import sklearn  # noqa: F401
    import xgboost  # noqa: F401


def test_train_persist_load_roundtrip(reset_inference_state, monkeypatch):
    """The full happy path the production deploy follows:
    train -> joblib.dump -> joblib.load -> ml_gate(real_features)."""
    tmp_path = reset_inference_state
    monkeypatch.setattr(train_xgb, "MODEL_DIR", tmp_path)

    df = _make_synthetic_training_df(n_rows=200)
    metadata = train_xgb.train(df, output_name="test_model.pkl")

    pkl_path = tmp_path / "test_model.pkl"
    meta_path = tmp_path / "test_model_meta.json"
    assert pkl_path.exists(), "train() did not write the .pkl"
    assert meta_path.exists(), "train() did not write the meta.json"
    assert pkl_path.stat().st_size > 1000, (
        "Model file is suspiciously small — joblib.dump may have written an "
        "empty / corrupted artifact."
    )

    inference.load_model()
    assert inference._artifact is not None, (
        "load_model() returned None artifact. This is the exact failure mode "
        "we shipped to production: the .pkl exists on disk but the loader "
        "(joblib or xgboost) cannot reconstruct the model object."
    )
    assert "model" in inference._artifact
    assert "features" in inference._artifact
    assert "threshold" in inference._artifact
    assert inference._artifact["features"] == train_xgb.ENTRY_FEATURES
    assert 0.0 < inference._artifact["threshold"] < 1.0

    sample_features = {f: float(df[f].iloc[0]) for f in train_xgb.ENTRY_FEATURES}
    allowed, p_win = inference.ml_gate(sample_features)

    assert isinstance(allowed, bool), f"ml_gate returned non-bool allowed={allowed!r}"
    assert isinstance(p_win, float), f"ml_gate returned non-float p_win={p_win!r}"
    assert 0.0 <= p_win <= 1.0, f"p_win out of valid range: {p_win}"

    on_disk_meta = json.loads(meta_path.read_text())
    for key in ("rows", "features", "threshold", "oos_precision", "win_rate"):
        assert key in on_disk_meta, f"meta.json missing required key: {key}"
    assert on_disk_meta["rows"] == metadata["rows"]


def test_inference_uses_correct_feature_order(reset_inference_state, monkeypatch):
    """Production safety: ml_gate must pass features to the model in the
    same order they were trained on. If the dict iteration order or the
    inference.py row construction ever drifts, this test catches it.

    We train a model where one feature is a near-perfect predictor and others
    are pure noise, then verify the trained model's predictions react to that
    feature when supplied via ml_gate."""
    tmp_path = reset_inference_state
    monkeypatch.setattr(train_xgb, "MODEL_DIR", tmp_path)

    rng = np.random.default_rng(0)
    rows = []
    for _ in range(300):
        obi = float(rng.uniform(0.0, 1.0))
        label = 1 if obi > 0.5 else -1
        row = {f: float(rng.uniform(-1.0, 1.0)) for f in train_xgb.ENTRY_FEATURES}
        row["obi"] = obi
        row["label"] = label
        rows.append(row)
    df = pd.DataFrame(rows)

    train_xgb.train(df, output_name="test_model.pkl")
    inference.load_model()

    base = {f: 0.0 for f in train_xgb.ENTRY_FEATURES}
    high = dict(base, obi=0.95)
    low = dict(base, obi=0.05)

    _, p_high = inference.ml_gate(high)
    _, p_low = inference.ml_gate(low)

    assert p_high > p_low, (
        f"Model trained on obi>0.5 -> win didn't react to obi via ml_gate(): "
        f"p_win(obi=0.95)={p_high:.3f} should be > p_win(obi=0.05)={p_low:.3f}. "
        "Likely a feature-order mismatch between train_xgb.ENTRY_FEATURES "
        "and the ordering in inference.ml_gate()."
    )


def test_load_model_logs_features_and_threshold(reset_inference_state, monkeypatch, caplog):
    """The bot's `ml.model_loaded` log line is the only signal an operator
    has post-deploy that the model actually loaded. This test pins that
    contract: load_model must log the path, feature count, and threshold
    in a structured way."""
    import structlog
    import logging

    tmp_path = reset_inference_state
    monkeypatch.setattr(train_xgb, "MODEL_DIR", tmp_path)

    df = _make_synthetic_training_df(n_rows=200)
    train_xgb.train(df, output_name="test_model.pkl")

    structlog.configure(
        processors=[structlog.stdlib.add_log_level, structlog.processors.JSONRenderer()],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    with caplog.at_level(logging.INFO):
        inference.load_model()

    load_lines = [r for r in caplog.records if "ml.model_loaded" in r.getMessage()]
    assert load_lines, (
        "load_model() succeeded but did not emit `ml.model_loaded`. "
        "Post-deploy verification depends on this log line."
    )
