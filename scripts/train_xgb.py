"""
Offline XGBoost training script for the ML entry gate.

Usage:
    # Export data from DB first:
    #   ssh botuser@64.23.133.157 "docker exec kbtc-db psql -U kalshi -d kbtc \
    #     -c \"COPY (SELECT * FROM trade_features WHERE label IS NOT NULL) TO STDOUT CSV HEADER\"" \
    #     > trade_features_export.csv
    #
    # Then train:
    #   python scripts/train_xgb.py --csv trade_features_export.csv

    # Or connect directly to the DB:
    #   python scripts/train_xgb.py --db-url postgresql://kalshi:kalshi_secret@localhost:5432/kbtc
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import (
    classification_report,
    precision_recall_curve,
)
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from xgboost import XGBClassifier

MODEL_DIR = Path(__file__).resolve().parent.parent / "backend" / "ml" / "models"

ENTRY_FEATURES = [
    "obi", "roc_3", "roc_5", "roc_10",
    "atr_pct", "spread_pct", "bid_depth", "ask_depth",
    "green_candles_3", "candle_body_pct", "volume_ratio",
    "time_remaining_sec", "hour_of_day", "day_of_week",
]

TRAINING_FEATURES = ENTRY_FEATURES + [
    "max_favorable_excursion",
    "max_adverse_excursion",
]


def load_data(csv_path: str | None = None, db_url: str | None = None) -> pd.DataFrame:
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
    required = ["label"] + ENTRY_FEATURES
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns: {missing}")
    return df


def train(df: pd.DataFrame, output_name: str = "xgb_entry_v1.pkl") -> dict:
    df["binary_label"] = (df["label"] == 1).astype(int)

    available_features = [f for f in TRAINING_FEATURES if f in df.columns]
    has_mfe_mae = "max_favorable_excursion" in df.columns and "max_adverse_excursion" in df.columns
    if has_mfe_mae:
        mfe_null_pct = df["max_favorable_excursion"].isna().mean()
        if mfe_null_pct > 0.5:
            print(f"WARNING: {mfe_null_pct:.0%} of MFE values are NULL, excluding MFE/MAE from features")
            available_features = ENTRY_FEATURES

    X = df[available_features].fillna(0)
    y = df["binary_label"]

    print(f"\n{'=' * 60}")
    print(f"Training XGBoost entry gate")
    print(f"  Rows: {len(df)}")
    print(f"  Features: {len(available_features)}")
    print(f"  Win rate: {y.mean():.1%}")
    print(f"  MFE/MAE available: {has_mfe_mae and 'max_favorable_excursion' in available_features}")
    print(f"{'=' * 60}\n")

    model = XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        eval_metric="logloss",
        random_state=42,
    )

    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)

    y_proba = cross_val_predict(model, X, y, cv=skf, method="predict_proba")[:, 1]

    precision, recall, thresholds = precision_recall_curve(y, y_proba)

    best_threshold = 0.5
    best_f1 = 0.0
    for p, r, t in zip(precision, recall, thresholds):
        if p < 0.58:
            continue
        f1 = 2 * p * r / (p + r) if (p + r) > 0 else 0
        if f1 > best_f1:
            best_f1 = f1
            best_threshold = float(t)

    y_pred = (y_proba >= best_threshold).astype(int)
    report = classification_report(y, y_pred, output_dict=True)

    oos_precision = report.get("1", {}).get("precision", 0)
    print(f"Optimal threshold: {best_threshold:.3f}")
    print(f"OOS precision (class 1): {oos_precision:.3f}")
    print(f"OOS recall (class 1): {report.get('1', {}).get('recall', 0):.3f}")
    print(f"\n{classification_report(y, y_pred)}")

    if oos_precision < 0.58:
        print(f"\nWARNING: OOS precision {oos_precision:.3f} < 0.58 threshold.")
        print("The signal may not be strong enough yet. Consider collecting more data.")

    model.fit(X, y)

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
        }
        joblib.dump(artifact, output_path)
    except ImportError:
        import pickle
        artifact = {
            "model": model,
            "features": available_features,
            "threshold": best_threshold,
        }
        with open(output_path, "wb") as f:
            pickle.dump(artifact, f)

    print(f"\nModel saved to {output_path}")

    metadata = {
        "rows": len(df),
        "features": available_features,
        "threshold": best_threshold,
        "oos_precision": round(oos_precision, 4),
        "oos_recall": round(report.get("1", {}).get("recall", 0), 4),
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
    args = parser.parse_args()

    if not args.csv and not args.db_url:
        print("ERROR: Provide either --csv or --db-url", file=sys.stderr)
        sys.exit(1)

    df = load_data(csv_path=args.csv, db_url=args.db_url)
    if len(df) < 100:
        print(f"WARNING: Only {len(df)} labeled rows. Recommended minimum is 500.", file=sys.stderr)

    train(df, output_name=args.output)


if __name__ == "__main__":
    main()
