"""Train/serve feature-contract tests.

Guards the BUG-class regression where the XGBoost trainer's feature list
included outcome columns (max_favorable_excursion / max_adverse_excursion)
that are populated only at trade EXIT. Effects of that bug:

  1. Massive label leakage during training -> inflated, misleading OOS metrics.
  2. Silent feature substitution at inference time: feature_capture.extract_features()
     can't produce those keys at entry, so inference.py defaulted them to 0
     for every live trade, fully de-tuning the model.

These tests enforce the only correct invariant: every feature the model is
trained on must be a key produced by extract_features() at entry-decision time.

The trainer's ENTRY_FEATURES list is parsed via AST so this test does NOT need
sklearn/xgboost installed (those are training-only deps, not bot runtime deps).
"""
from __future__ import annotations

import ast
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TRAINER_PATH = REPO_ROOT / "scripts" / "train_xgb.py"

# Outcome / label columns. Adding any of these to ENTRY_FEATURES is a bug:
# they are written to trade_features only at exit by ml.feature_capture.label_trade
# and therefore leak the label during training and are pinned to 0 at inference.
OUTCOME_BLOCKLIST = {
    "max_favorable_excursion",
    "max_adverse_excursion",
    "label",
    "pnl",
    "binary_label",
}


def _read_entry_features_via_ast() -> list[str]:
    """Parse ENTRY_FEATURES out of scripts/train_xgb.py without importing it.

    Importing would pull in sklearn + xgboost, which are training-only
    dependencies not present in the bot's runtime venv / CI image.
    """
    assert TRAINER_PATH.exists(), f"trainer file not found: {TRAINER_PATH}"
    tree = ast.parse(TRAINER_PATH.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t for t in node.targets if isinstance(t, ast.Name)]
        if not any(t.id == "ENTRY_FEATURES" for t in targets):
            continue
        if not isinstance(node.value, (ast.List, ast.Tuple)):
            raise AssertionError("ENTRY_FEATURES must be a literal list/tuple")
        names: list[str] = []
        for elt in node.value.elts:
            if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
                raise AssertionError(
                    "ENTRY_FEATURES must contain only string literals "
                    "so this contract test can verify it statically."
                )
            names.append(elt.value)
        return names
    raise AssertionError("ENTRY_FEATURES not found in scripts/train_xgb.py")


def _call_extract_features() -> dict:
    """Invoke ml.feature_capture.extract_features with permissive mocks.

    We don't care what the values are -- only what KEYS appear in the returned
    dict, which is the live entry-time feature surface.
    """
    # MFE/MAE removal is the whole point of this test: don't let an old cached
    # train_xgb module masquerade as the real one. Drop any prior import.
    sys.modules.pop("ml.feature_capture", None)
    from ml.feature_capture import extract_features  # noqa: WPS433

    fake_candle = types.SimpleNamespace(
        open=100.0, high=101.0, low=99.0, close=100.5, volume=10.0
    )
    candle_aggregator = MagicMock()
    candle_aggregator.recent.return_value = [fake_candle] * 10

    features = MagicMock()
    features.obi = 0.5
    features.obi_raw = 0.5
    features.spread_cents = 1
    features.mid_price = 50
    features.total_bid_vol = 100
    features.total_ask_vol = 100

    atr_filter = types.SimpleNamespace(atr_pct_history=[0.01])

    state = types.SimpleNamespace(time_remaining_sec=300, kalshi_ticker="KXBTC-X")

    return extract_features(
        features=features,
        candle_aggregator=candle_aggregator,
        atr_filter=atr_filter,
        state=state,
        historical_sync=None,
    )


def test_entry_features_subset_of_extract_features():
    """Every name in ENTRY_FEATURES must be a key returned by extract_features().

    This is the train/serve contract. If this fails, the next training run
    will produce a model whose feature list does not match what the bot can
    produce at entry time, and inference.py will silently substitute 0 for
    the missing keys -- the exact failure mode of BUG-026.
    """
    entry_features = set(_read_entry_features_via_ast())
    served_keys = set(_call_extract_features().keys())
    missing = entry_features - served_keys
    assert not missing, (
        f"ENTRY_FEATURES contains names not produced by extract_features() at "
        f"entry: {sorted(missing)}. Either remove them from the trainer or add "
        f"them to backend/ml/feature_capture.py::extract_features."
    )


def test_no_outcome_columns_in_entry_features():
    """Outcome columns (label, pnl, MFE, MAE) must never appear in ENTRY_FEATURES.

    They are populated only at trade exit by ml.feature_capture.label_trade and
    cause label leakage at training time + silent zero-substitution at inference.
    Append to OUTCOME_BLOCKLIST when new outcome columns are introduced.
    """
    entry_features = set(_read_entry_features_via_ast())
    leaked = entry_features & OUTCOME_BLOCKLIST
    assert not leaked, (
        f"ENTRY_FEATURES contains outcome columns: {sorted(leaked)}. "
        "These are populated only at trade exit and create label leakage. "
        "See BUG-026 in .cursor/rules/known-bugs.mdc."
    )
