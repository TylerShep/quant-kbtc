"""
Offline XGBoost training script for the ML entry gate.

Usage:
    # Export data from DB first (set KBTC_DEPLOY_HOST=user@host):
    #   ssh "$KBTC_DEPLOY_HOST" "docker exec kbtc-db psql -U kalshi -d kbtc \
    #     -c \"COPY (SELECT * FROM trade_features WHERE label IS NOT NULL) TO STDOUT CSV HEADER\"" \
    #     > trade_features_export.csv
    #
    # Then train:
    #   python scripts/train_xgb.py --csv trade_features_export.csv

    # Or connect directly to the DB:
    #   python scripts/train_xgb.py --db-url postgresql://kalshi:kalshi_secret@localhost:5432/kbtc

Feature invariant (READ BEFORE EDITING ENTRY_FEATURES):
    Every name in ENTRY_FEATURES must be a key in the dict returned by
    `backend/ml/feature_capture.py::extract_features()` at trade-entry time.
    NEVER add columns that are populated only at trade exit (e.g. label, pnl,
    max_favorable_excursion, max_adverse_excursion) -- those are outcome data
    and including them creates label leakage during training plus silent
    `feature_dict.get(..., 0)` substitution during live inference.
    The contract is enforced by `backend/tests/test_train_serve_features.py`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import (
    brier_score_loss,
    classification_report,
    precision_recall_curve,
)
from sklearn.model_selection import StratifiedKFold, TimeSeriesSplit, cross_val_predict
from xgboost import XGBClassifier

MODEL_DIR = Path(__file__).resolve().parent.parent / "backend" / "ml" / "models"

ENTRY_FEATURES = [
    "obi", "roc_3", "roc_5", "roc_10",
    "atr_pct", "spread_pct", "bid_depth", "ask_depth",
    "green_candles_3", "candle_body_pct", "volume_ratio",
    "time_remaining_sec", "hour_of_day", "day_of_week",
    # v2 execution-quality features (Tier 1.b, 2026-04-28). They go live
    # as soon as ENTRY_FEATURES references them, but the ML model in
    # production was trained without them and so will treat them as zero
    # at inference time (fail-open via ml_gate's feature_dict.get(f, 0)).
    # The next retrain after enough rows accumulate (~7 days) will
    # produce xgb_entry_v2 that actually uses them. See migration 008
    # and docs/runbooks/ml-retraining.md.
    "minutes_to_contract_close", "quoted_spread_at_entry_bps",
    "book_thickness_at_offer", "recent_trade_count_60s",
]


# Minimum rows required when training a LIVE-only model. Below this, the
# 5-fold CV used in train() will produce wildly unstable metrics and any
# precision claim from the resulting model is noise. The number is
# deliberately conservative — increase as the live tail grows. Tracked
# alongside the >=200 MIN_ROWS used by retrain_promote for the default
# (both-mode) trainer.
MIN_LIVE_ROWS_FOR_LIVE_MODE = 50
MIN_PRECISION_FLOOR = 0.58


def _build_model() -> XGBClassifier:
    return XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        eval_metric="logloss",
        random_state=42,
    )


def _pick_threshold(
    y_true: np.ndarray,
    y_proba: np.ndarray,
    *,
    min_precision: float,
) -> tuple[float, float, float, float]:
    precision, recall, thresholds = precision_recall_curve(y_true, y_proba)

    best_threshold = 0.5
    best_f1 = 0.0
    best_precision = 0.0
    best_recall = 0.0
    for p, r, t in zip(precision[:-1], recall[:-1], thresholds):
        if p < min_precision:
            continue
        f1 = 2 * p * r / (p + r) if (p + r) > 0 else 0
        if f1 > best_f1:
            best_f1 = f1
            best_threshold = float(t)
            best_precision = float(p)
            best_recall = float(r)

    if best_f1 == 0.0:
        y_pred_default = (y_proba >= best_threshold).astype(int)
        report_default = classification_report(
            y_true, y_pred_default, output_dict=True, zero_division=0
        )
        best_precision = float(report_default.get("1", {}).get("precision", 0.0))
        best_recall = float(report_default.get("1", {}).get("recall", 0.0))

    return best_threshold, best_precision, best_recall, best_f1


def load_data(
    csv_path: str | None = None,
    db_url: str | None = None,
    mode: str = "both",
) -> pd.DataFrame:
    """Load labeled feature rows.

    Args:
        csv_path: Path to a pre-exported CSV.
        db_url: Postgres URL.
        mode: One of ``"paper"``, ``"live"``, or ``"both"``. The latter
            is the historical default and trains on the full mixed
            dataset. ``"paper"`` and ``"live"`` filter by
            ``trading_mode``, which lets us A/B paper-trained vs
            live-trained models against each other once the live tail
            has enough rows. ``"live"`` raises if the resulting frame has
            fewer than ``MIN_LIVE_ROWS_FOR_LIVE_MODE`` rows so an
            operator doesn't accidentally ship a model trained on
            ten samples.
    """
    if mode not in ("paper", "live", "both"):
        raise ValueError(f"mode must be 'paper', 'live', or 'both' (got {mode!r})")

    if csv_path:
        df = pd.read_csv(csv_path)
    elif db_url:
        from sqlalchemy import create_engine
        engine = create_engine(db_url)
        df = pd.read_sql(
            "SELECT * FROM trade_features WHERE label IS NOT NULL",
            engine,
        )
    else:
        raise ValueError("Provide either --csv or --db-url")

    df = df[df["label"].notna()].copy()

    if mode != "both":
        if "trading_mode" not in df.columns:
            raise ValueError(
                f"mode={mode!r} requires a 'trading_mode' column in the source. "
                "Re-export from trade_features (the column is included by default)."
            )
        before = len(df)
        df = df[df["trading_mode"] == mode].copy()
        print(f"  mode filter: {mode!r} -> {len(df)} rows (from {before})")
        if mode == "live" and len(df) < MIN_LIVE_ROWS_FOR_LIVE_MODE:
            raise ValueError(
                f"--mode live requires at least {MIN_LIVE_ROWS_FOR_LIVE_MODE} "
                f"labeled rows, but only {len(df)} are available. The live tail "
                "is too small for a stable 5-fold CV. Run "
                "scripts/backfill_live_trade_features.py and let more live trades "
                "accumulate before retrying."
            )

    required = ["label"] + ENTRY_FEATURES
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns: {missing}")
    return df


def train(
    df: pd.DataFrame,
    output_name: str = "xgb_entry_v1.pkl",
    *,
    training_mode: str = "both",
    eval_mode: str = "stratified_kfold",
    time_holdout_fraction: float = 0.20,
    time_splits: int = 5,
) -> dict:
    """Train an XGBoost entry-gate model on ``df``.

    ``training_mode`` is recorded in the artifact and in the meta JSON for
    downstream visibility (so retrain_promote can compare like-for-like
    incumbent vs candidate, and so the bot can log which mode the loaded
    model was trained on at startup). Defaults to ``"both"`` for backward
    compatibility with the existing cron / promotion pipeline.
    """
    df["binary_label"] = (df["label"] == 1).astype(int)

    missing = [f for f in ENTRY_FEATURES if f not in df.columns]
    if missing:
        raise ValueError(
            f"Training data is missing required entry-time columns: {missing}. "
            "Run the migration / re-export trade_features."
        )
    available_features = list(ENTRY_FEATURES)
    X = df[available_features].fillna(0)
    y = df["binary_label"].astype(int)

    print(f"\n{'=' * 60}")
    print(f"Training XGBoost entry gate")
    print(f"  Rows: {len(df)}")
    print(f"  Features: {len(available_features)} (entry-time only)")
    print(f"  Win rate: {y.mean():.1%}")
    print(f"{'=' * 60}\n")

    if eval_mode not in {"stratified_kfold", "time_ordered"}:
        raise ValueError(f"Unknown eval_mode {eval_mode!r}")

    threshold_selection_precision = 0.0
    threshold_selection_recall = 0.0
    threshold_selection_f1 = 0.0
    threshold_selection_rows = len(df)
    holdout_rows = 0
    holdout_precision = 0.0
    holdout_recall = 0.0
    oos_brier = 0.0
    promotion_metric_source = "cv"

    if eval_mode == "stratified_kfold":
        model = _build_model()
        skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
        y_proba = cross_val_predict(model, X, y, cv=skf, method="predict_proba")[:, 1]
        best_threshold, threshold_selection_precision, threshold_selection_recall, threshold_selection_f1 = (
            _pick_threshold(
                y.to_numpy(),
                y_proba,
                min_precision=MIN_PRECISION_FLOOR,
            )
        )
        y_pred = (y_proba >= best_threshold).astype(int)
        report = classification_report(
            y,
            y_pred,
            output_dict=True,
            zero_division=0,
        )
        oos_precision = float(report.get("1", {}).get("precision", 0))
        oos_recall = float(report.get("1", {}).get("recall", 0))
        oos_brier = float(brier_score_loss(y, y_proba))
        print(f"Threshold source: stratified CV predictions ({len(y)} rows)")
        print(f"Optimal threshold: {best_threshold:.3f}")
        print(f"OOS precision (class 1): {oos_precision:.3f}")
        print(f"OOS recall (class 1): {oos_recall:.3f}")
        print(f"OOS brier score: {oos_brier:.4f}")
        print(f"\n{classification_report(y, y_pred, zero_division=0)}")
        model.fit(X, y)
    else:
        if "timestamp" not in df.columns:
            raise ValueError(
                "time_ordered eval mode requires a 'timestamp' column in trade_features."
            )
        ordered = df.copy()
        ordered["timestamp"] = pd.to_datetime(
            ordered["timestamp"],
            utc=True,
            errors="coerce",
        )
        ordered = ordered[ordered["timestamp"].notna()].sort_values("timestamp").reset_index(drop=True)
        if len(ordered) < 200:
            raise ValueError(
                "time_ordered eval mode needs at least 200 rows after timestamp sort."
            )
        holdout_size = max(50, int(len(ordered) * max(0.10, min(0.40, time_holdout_fraction))))
        train_size = len(ordered) - holdout_size
        if train_size < 100:
            raise ValueError(
                "Not enough rows left for threshold-tuning train split after holdout carve-out."
            )

        train_df = ordered.iloc[:train_size].copy()
        holdout_df = ordered.iloc[train_size:].copy()
        X_train = train_df[available_features].fillna(0)
        y_train = train_df["binary_label"].astype(int)
        X_holdout = holdout_df[available_features].fillna(0)
        y_holdout = holdout_df["binary_label"].astype(int)

        split_count = max(2, min(time_splits, 8))
        tss = TimeSeriesSplit(n_splits=split_count)
        val_probs: list[np.ndarray] = []
        val_labels: list[np.ndarray] = []
        for train_idx, val_idx in tss.split(X_train):
            y_train_fold = y_train.iloc[train_idx]
            if y_train_fold.nunique() < 2:
                continue
            fold_model = _build_model()
            fold_model.fit(X_train.iloc[train_idx], y_train_fold)
            fold_probs = fold_model.predict_proba(X_train.iloc[val_idx])[:, 1]
            val_probs.append(fold_probs)
            val_labels.append(y_train.iloc[val_idx].to_numpy())

        if not val_probs:
            split_at = int(len(X_train) * 0.75)
            if split_at <= 0 or split_at >= len(X_train):
                raise ValueError("Unable to create fallback forward validation split.")
            fallback_model = _build_model()
            fallback_model.fit(X_train.iloc[:split_at], y_train.iloc[:split_at])
            val_probs = [fallback_model.predict_proba(X_train.iloc[split_at:])[:, 1]]
            val_labels = [y_train.iloc[split_at:].to_numpy()]

        y_val = np.concatenate(val_labels)
        p_val = np.concatenate(val_probs)
        threshold_selection_rows = int(len(y_val))
        best_threshold, threshold_selection_precision, threshold_selection_recall, threshold_selection_f1 = (
            _pick_threshold(y_val, p_val, min_precision=MIN_PRECISION_FLOOR)
        )

        promotion_metric_source = "holdout"
        model = _build_model()
        model.fit(X_train, y_train)
        holdout_proba = model.predict_proba(X_holdout)[:, 1]
        holdout_pred = (holdout_proba >= best_threshold).astype(int)
        holdout_report_dict = classification_report(
            y_holdout,
            holdout_pred,
            output_dict=True,
            zero_division=0,
        )
        holdout_rows = int(len(y_holdout))
        holdout_precision = float(holdout_report_dict.get("1", {}).get("precision", 0.0))
        holdout_recall = float(holdout_report_dict.get("1", {}).get("recall", 0.0))
        oos_brier = float(brier_score_loss(y_holdout, holdout_proba))
        oos_precision = holdout_precision
        oos_recall = holdout_recall
        print("Threshold source: forward-only validation from pre-holdout segment")
        print(f"Threshold tuning rows: {threshold_selection_rows}")
        print(f"Holdout rows: {holdout_rows}")
        print(f"Optimal threshold: {best_threshold:.3f}")
        print(f"Holdout precision (class 1): {holdout_precision:.3f}")
        print(f"Holdout recall (class 1): {holdout_recall:.3f}")
        print(f"Holdout brier score: {oos_brier:.4f}")
        print(f"\n{classification_report(y_holdout, holdout_pred, zero_division=0)}")

    if oos_precision < MIN_PRECISION_FLOOR:
        print(f"\nWARNING: OOS precision {oos_precision:.3f} < {MIN_PRECISION_FLOOR:.2f} threshold.")
        print("The signal may not be strong enough yet. Consider collecting more data.")

    importance = dict(zip(available_features, model.feature_importances_.tolist()))
    sorted_imp = sorted(importance.items(), key=lambda x: x[1], reverse=True)
    print("\nFeature importance:")
    for feat, imp in sorted_imp:
        print(f"  {feat:30s} {imp:.4f}")

    top_feat_pct = sorted_imp[0][1] if sorted_imp else 0
    if top_feat_pct > 0.40:
        print(f"\nWARNING: Top feature '{sorted_imp[0][0]}' dominates at {top_feat_pct:.0%} importance.")

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    output_path = MODEL_DIR / output_name

    try:
        import joblib
        artifact = {
            "model": model,
            "features": available_features,
            "threshold": best_threshold,
            "training_mode": training_mode,
            "threshold_source": promotion_metric_source,
            "eval_mode": eval_mode,
        }
        joblib.dump(artifact, output_path)
    except ImportError:
        import pickle
        artifact = {
            "model": model,
            "features": available_features,
            "threshold": best_threshold,
            "training_mode": training_mode,
            "threshold_source": promotion_metric_source,
            "eval_mode": eval_mode,
        }
        with open(output_path, "wb") as f:
            pickle.dump(artifact, f)

    print(f"\nModel saved to {output_path}")

    metadata = {
        "rows": len(df),
        "features": available_features,
        "threshold": best_threshold,
        "training_mode": training_mode,
        "eval_mode": eval_mode,
        "threshold_source": promotion_metric_source,
        "threshold_selection_rows": threshold_selection_rows,
        "threshold_selection_precision": round(threshold_selection_precision, 4),
        "threshold_selection_recall": round(threshold_selection_recall, 4),
        "threshold_selection_f1": round(threshold_selection_f1, 4),
        "holdout_rows": holdout_rows,
        "holdout_precision": round(holdout_precision, 4),
        "holdout_recall": round(holdout_recall, 4),
        "oos_precision": round(oos_precision, 4),
        "oos_recall": round(oos_recall, 4),
        "oos_brier": round(oos_brier, 6),
        "win_rate": round(float(y.mean()), 4),
        "feature_importance": {k: round(v, 4) for k, v in sorted_imp},
    }
    meta_path = MODEL_DIR / output_name.replace(".pkl", "_meta.json")
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)
    print(f"Metadata saved to {meta_path}")

    return metadata


def main():
    parser = argparse.ArgumentParser(description="Train XGBoost entry gate model")
    parser.add_argument("--csv", help="Path to trade_features CSV export")
    parser.add_argument("--db-url", help="Database connection URL")
    parser.add_argument("--output", default="xgb_entry_v1.pkl", help="Output model filename")
    parser.add_argument(
        "--mode",
        choices=("paper", "live", "both"),
        default="both",
        help=(
            "Filter trade_features by trading_mode before training. "
            "'both' (default) preserves the historical behaviour and trains on "
            "the full mixed dataset. 'paper' / 'live' enable A/B comparison "
            "between paper-trained and live-trained models. 'live' enforces a "
            f"minimum of {MIN_LIVE_ROWS_FOR_LIVE_MODE} labeled rows so an "
            "operator can't accidentally ship a model trained on noise."
        ),
    )
    parser.add_argument(
        "--eval-mode",
        choices=("stratified_kfold", "time_ordered"),
        default="stratified_kfold",
        help=(
            "Evaluation protocol: stratified_kfold (legacy) or time_ordered "
            "(forward-only threshold tuning + held-out final scoring)."
        ),
    )
    parser.add_argument(
        "--time-holdout-fraction",
        type=float,
        default=0.20,
        help="Fraction of newest rows reserved as final holdout in time_ordered mode.",
    )
    parser.add_argument(
        "--time-splits",
        type=int,
        default=5,
        help="Forward splits used for threshold tuning in time_ordered mode.",
    )
    args = parser.parse_args()

    if not args.csv and not args.db_url:
        print("ERROR: Provide either --csv or --db-url", file=sys.stderr)
        sys.exit(1)

    df = load_data(csv_path=args.csv, db_url=args.db_url, mode=args.mode)
    if len(df) < 100:
        print(f"WARNING: Only {len(df)} labeled rows. Recommended minimum is 500.", file=sys.stderr)

    train(
        df,
        output_name=args.output,
        training_mode=args.mode,
        eval_mode=args.eval_mode,
        time_holdout_fraction=args.time_holdout_fraction,
        time_splits=args.time_splits,
    )


if __name__ == "__main__":
    main()
