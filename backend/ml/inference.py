"""
XGBoost entry gate — loaded once at startup, called per trade signal.

Fail-open design: if the model file is missing, fails to load, or throws
at inference time, ml_gate() returns (True, 0.5) so the trade proceeds
as if the gate was not present.
"""
from __future__ import annotations

from pathlib import Path
from typing import Tuple

import numpy as np
import structlog

from config import settings

logger = structlog.get_logger()

_MODEL_PATH = Path(__file__).parent / "models" / "xgb_entry_v1.pkl"
_artifact = None
_threshold: float = 0.55
_threshold_source_resolved: str = "default"


def _resolve_threshold(artifact: dict) -> tuple[float, str]:
    artifact_threshold = float(artifact.get("threshold", 0.55))
    source = str(getattr(settings.ml, "threshold_source", "artifact")).strip().lower()
    if source == "config":
        configured = float(getattr(settings.ml, "min_p_win", 0.0))
        if configured <= 0.0:
            return artifact_threshold, "artifact_fallback_from_config"
        return max(0.0, min(1.0, configured)), "config"
    return max(0.0, min(1.0, artifact_threshold)), "artifact"


def load_model() -> None:
    global _artifact, _threshold, _threshold_source_resolved
    if not _MODEL_PATH.exists():
        logger.warning("ml.model_not_found", path=str(_MODEL_PATH))
        return
    try:
        try:
            import joblib
            _artifact = joblib.load(_MODEL_PATH)
        except ImportError:
            import pickle
            with open(_MODEL_PATH, "rb") as f:
                _artifact = pickle.load(f)
        _threshold, _threshold_source_resolved = _resolve_threshold(_artifact)
        logger.info("ml.model_loaded", path=str(_MODEL_PATH),
                     features=len(_artifact.get("features", [])),
                     threshold=_threshold,
                     threshold_source=_threshold_source_resolved,
                     training_mode=_artifact.get("training_mode", "unknown"))
    except Exception as e:
        logger.error("ml.model_load_failed", error=str(e))
        _artifact = None


def ml_gate(feature_dict: dict) -> Tuple[bool, float]:
    """Returns (allowed, p_win).

    If no model is loaded, always returns (True, 0.5) — passthrough.
    """
    if _artifact is None:
        return True, 0.5

    try:
        model = _artifact["model"]
        features = _artifact["features"]
        row = np.array([[feature_dict.get(f, 0) or 0 for f in features]])
        p_win = float(model.predict_proba(row)[0][1])
        return p_win >= _threshold, p_win
    except Exception as e:
        logger.warning("ml.gate_inference_error", error=str(e))
        return True, 0.5
