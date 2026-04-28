"""Tests for the --mode {paper,live,both} flag in scripts/train_xgb.py.

These exercise the data-filtering / guard logic in ``load_data`` only.
We deliberately don't import the trainer's ``train()`` function in tests
because xgboost + sklearn are training-only deps not present in the
bot's runtime venv (per the comment in test_train_serve_features.py).
For ``load_data`` the only required dep is pandas, which the runtime
venv does not have either — so we test against a CSV path with a
preconstructed dataframe-shaped file.

The --mode flag was added 2026-04-28 (Tier 1.c) to enable
A/B-comparing a paper-trained model vs a live-trained model on the
held-out live tail. The default 'both' preserves the historical mixed-
training behaviour. The 'live' mode enforces a minimum row count so an
operator can't accidentally ship a model trained on a handful of rows.
"""
from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TRAINER_PATH = REPO_ROOT / "scripts" / "train_xgb.py"

# Skip the whole file when pandas (a training-only dep) isn't present.
# The unit test for the train/serve feature *contract* uses AST parsing
# precisely to avoid this dep; this file's tests need actual runtime
# execution of the dataframe filter, so pandas IS required.
pd = pytest.importorskip("pandas", reason="pandas is a training-only dep")


def _load_train_xgb():
    """Import train_xgb as a module without running its ``main()``.

    importlib lets us load it from outside the package tree without a
    sys.path mutation. We do NOT trigger the xgboost-importing code
    paths (that's all inside ``train()`` which we don't call here)."""
    spec = importlib.util.spec_from_file_location("train_xgb_under_test", TRAINER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["train_xgb_under_test"] = module
    spec.loader.exec_module(module)
    return module


def _write_csv(path: Path, rows: list[dict]) -> None:
    """Write a small trade_features-shaped CSV. Every row must have at
    least the columns referenced by load_data's missing-cols check
    (label + ENTRY_FEATURES + trading_mode)."""
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _row(trading_mode: str, label: int) -> dict:
    """One synthetic feature row. All ENTRY_FEATURES present and non-null."""
    return {
        "trading_mode": trading_mode,
        "label": label,
        "obi": 0.5,
        "roc_3": 0.001,
        "roc_5": 0.002,
        "roc_10": 0.003,
        "atr_pct": 0.01,
        "spread_pct": 0.04,
        "bid_depth": 100,
        "ask_depth": 100,
        "green_candles_3": 2,
        "candle_body_pct": 0.5,
        "volume_ratio": 1.2,
        "time_remaining_sec": 600,
        "hour_of_day": 14,
        "day_of_week": 1,
        "minutes_to_contract_close": 10.0,
        "quoted_spread_at_entry_bps": 400,
        "book_thickness_at_offer": 500,
        "recent_trade_count_60s": 5,
    }


def test_default_mode_both_loads_all_rows(tmp_path: Path):
    """The historical default (no --mode flag, mode='both') must
    return every labeled row regardless of trading_mode. Existing cron
    pipelines depend on this."""
    csv_path = tmp_path / "f.csv"
    _write_csv(csv_path, [_row("paper", 1) for _ in range(10)] + [_row("live", -1) for _ in range(5)])

    train_xgb = _load_train_xgb()
    df = train_xgb.load_data(csv_path=str(csv_path), mode="both")
    assert len(df) == 15


def test_mode_paper_filters_to_paper_only(tmp_path: Path):
    csv_path = tmp_path / "f.csv"
    _write_csv(csv_path, [_row("paper", 1) for _ in range(7)] + [_row("live", -1) for _ in range(3)])

    train_xgb = _load_train_xgb()
    df = train_xgb.load_data(csv_path=str(csv_path), mode="paper")
    assert len(df) == 7
    assert (df["trading_mode"] == "paper").all()


def test_mode_live_raises_when_below_threshold(tmp_path: Path):
    """Below MIN_LIVE_ROWS_FOR_LIVE_MODE the loader must refuse rather
    than silently train a noise model. The error message must mention
    the required minimum and tell the operator what to do."""
    csv_path = tmp_path / "f.csv"
    _write_csv(csv_path, [_row("paper", 1) for _ in range(100)] + [_row("live", -1) for _ in range(5)])

    train_xgb = _load_train_xgb()
    with pytest.raises(ValueError) as exc_info:
        train_xgb.load_data(csv_path=str(csv_path), mode="live")
    msg = str(exc_info.value)
    assert "live" in msg
    assert str(train_xgb.MIN_LIVE_ROWS_FOR_LIVE_MODE) in msg
    assert "backfill_live_trade_features.py" in msg


def test_mode_live_succeeds_at_threshold(tmp_path: Path):
    """Exactly at the MIN_LIVE_ROWS_FOR_LIVE_MODE threshold the loader
    must succeed. (Strictly less than fails; equal passes.)"""
    csv_path = tmp_path / "f.csv"
    train_xgb = _load_train_xgb()
    n = train_xgb.MIN_LIVE_ROWS_FOR_LIVE_MODE
    _write_csv(csv_path, [_row("live", 1) for _ in range(n)])
    df = train_xgb.load_data(csv_path=str(csv_path), mode="live")
    assert len(df) == n


def test_invalid_mode_raises_value_error(tmp_path: Path):
    """mode must be one of 'paper', 'live', 'both'. An unknown string
    must surface immediately rather than silently fall through to the
    no-filter path."""
    csv_path = tmp_path / "f.csv"
    _write_csv(csv_path, [_row("paper", 1) for _ in range(10)])

    train_xgb = _load_train_xgb()
    with pytest.raises(ValueError, match="mode must be"):
        train_xgb.load_data(csv_path=str(csv_path), mode="canary")


def test_mode_filter_requires_trading_mode_column(tmp_path: Path):
    """Loading from a CSV that doesn't have a ``trading_mode`` column
    is fine in 'both' mode (we never filter by it) but must fail loudly
    when --mode paper / --mode live is requested."""
    csv_path = tmp_path / "f.csv"
    rows = [_row("paper", 1) for _ in range(10)]
    for r in rows:
        r.pop("trading_mode")
    _write_csv(csv_path, rows)

    train_xgb = _load_train_xgb()
    df = train_xgb.load_data(csv_path=str(csv_path), mode="both")
    assert len(df) == 10

    with pytest.raises(ValueError, match="trading_mode"):
        train_xgb.load_data(csv_path=str(csv_path), mode="paper")
