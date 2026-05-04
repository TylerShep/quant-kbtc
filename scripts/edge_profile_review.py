"""Weekly edge_profile + ML co-calibration review.

Pulls the last N days of paper trades joined with their captured features,
optionally re-scores each row through the active ML model so attribution
reflects what the live lane actually sees, then computes per-gate
"protecting" vs "leaking" PnL and produces:

  1. A markdown report posted to Discord (DISCORD_ATTRIBUTION_WEBHOOK)
  2. A JSON sidecar of structured per-param recommendations consumed
     by scripts/edge_profile_apply.py for Tier 1 auto-apply

Per-recommendation tier tagging:
  AUTO_APPLY  -- strictly tightening, ≥30 supporting trades, |PnL| > $200,
                 not a kill switch param. Phase 2.5 may auto-apply these.
  MANUAL_ONLY -- everything else (loosening, kill-switch flips, low data).

Usage:
    python scripts/edge_profile_review.py \
        --window-days 14 \
        --mode paper \
        --post-discord \
        --output-dir /home/botuser/kbtc/data/edge_review

The script never mutates the live env or the bot itself. That is
exclusively scripts/edge_profile_apply.py's job, which reads the JSON
sidecar produced here.

Exit codes:
  0  review completed successfully
  1  fatal error (DB unreachable, malformed input, etc.)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE
_BACKEND_DIR = _HERE.parent / "backend"
if not _BACKEND_DIR.exists():
    _BACKEND_DIR = Path("/app")
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_BACKEND_DIR))


KILL_SWITCH_PARAMS = frozenset({
    "EDGE_LIVE_PROFILE_ENABLED",
    "EDGE_LIVE_LONG_ONLY",
    "EDGE_LIVE_AUTO_APPLY_ENABLED",
})

AUTO_APPLY_MIN_TRADES = 30
AUTO_APPLY_MIN_PNL_IMPACT = 200.0


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ─── Pure recommendation logic (testable) ─────────────────────────────────

def classify_recommendation(
    *,
    param: str,
    current: Any,
    suggested: Any,
    n_supporting_trades: int,
    pnl_impact_dollars: float,
    is_tightening: bool,
) -> str:
    """Return ``"AUTO_APPLY"`` or ``"MANUAL_ONLY"`` for a recommendation.

    Pure function; the unit tests cover every branch. Kept here rather
    than in the apply script because the review script is the source of
    truth for tier tagging.
    """
    if param in KILL_SWITCH_PARAMS:
        return "MANUAL_ONLY"
    if not is_tightening:
        return "MANUAL_ONLY"
    if current == suggested:
        return "MANUAL_ONLY"  # a no-op shouldn't be "auto-applied"
    if n_supporting_trades < AUTO_APPLY_MIN_TRADES:
        return "MANUAL_ONLY"
    if abs(pnl_impact_dollars) < AUTO_APPLY_MIN_PNL_IMPACT:
        return "MANUAL_ONLY"
    return "AUTO_APPLY"


def is_tightening_for_param(param: str, current: Any, suggested: Any) -> bool:
    """Determine whether ``current -> suggested`` is more restrictive.

    "Tightening" semantics differ per param:
      * SHORT_MIN_PRICE: higher = more restrictive (rejects more shorts)
      * SHORT_MIN_CONVICTION: HIGH > NORMAL > LOW (more restrictive)
      * MAX_ENTRY_PRICE: lower = more restrictive (caps more longs)
      * BLOCKED_HOURS_UTC: more hours = more restrictive
      * ALLOWED_DRIVERS: fewer drivers = more restrictive
      * BLOCK_LOW_CONVICTION: True > False (more restrictive)
      * SHORT_BLOCK_NEGATIVE_ROC_THRESHOLD: closer to 0 = more restrictive
        (rejects more shorts because more roc_5 values clear the bar). A
        non-negative value means "disabled"; moving from disabled to any
        negative value, or making the negative value less negative, both
        count as tightening.
    """
    if current is None or suggested is None:
        return False
    if param == "EDGE_LIVE_SHORT_MIN_PRICE":
        return float(suggested) > float(current)
    if param == "EDGE_LIVE_SHORT_MIN_CONVICTION":
        order = {"LOW": 1, "NORMAL": 2, "HIGH": 3}
        return order.get(str(suggested).upper(), 0) > order.get(str(current).upper(), 0)
    if param == "EDGE_LIVE_MAX_ENTRY_PRICE":
        return float(suggested) < float(current)
    if param == "EDGE_LIVE_BLOCKED_HOURS_UTC":
        cur_set = _parse_csv_int_set(current)
        new_set = _parse_csv_int_set(suggested)
        return cur_set.issubset(new_set) and cur_set != new_set
    if param == "EDGE_LIVE_ALLOWED_DRIVERS":
        cur_set = _parse_csv_str_set(current)
        new_set = _parse_csv_str_set(suggested)
        return new_set.issubset(cur_set) and cur_set != new_set
    if param == "EDGE_LIVE_BLOCK_LOW_CONVICTION":
        return _to_bool(suggested) and not _to_bool(current)
    if param == "EDGE_LIVE_SHORT_BLOCK_NEGATIVE_ROC_THRESHOLD":
        # "Disabled" = >= 0.0; any negative value is "active".
        cur_f = float(current)
        new_f = float(suggested)
        cur_active = cur_f < 0.0
        new_active = new_f < 0.0
        if not new_active:
            return False  # going to disabled = loosening
        if not cur_active:
            return True  # disabled -> active = tightening
        # Both active. Closer to 0 (less negative) blocks more trades.
        return new_f > cur_f
    return False


def _parse_csv_int_set(val: Any) -> set:
    if val is None or val == "":
        return set()
    if isinstance(val, (set, list, tuple)):
        return {int(v) for v in val if str(v).strip()}
    return {int(p) for p in str(val).split(",") if p.strip()}


def _parse_csv_str_set(val: Any) -> set:
    if val is None or val == "":
        return set()
    if isinstance(val, (set, list, tuple)):
        return {str(v).strip() for v in val if str(v).strip()}
    return {p.strip() for p in str(val).split(",") if p.strip()}


def _to_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("true", "1", "yes", "on")


# ─── Data load ────────────────────────────────────────────────────────────

def _normalize_db_url(url: str) -> str:
    """Force the SQLAlchemy URL to use psycopg v3 (the driver that's in
    the bot's container image). The default ``postgresql://`` dialect
    resolves to psycopg2 which isn't installed."""
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    return url


def load_trades_with_features(db_url: str, window_days: int, mode: str):
    """Pull trades joined with trade_features.

    The trade_features table stores the entry-time feature snapshot keyed
    by trade_id. We left-join so trades whose features were not captured
    (older trades, capture-failure rows) still appear with feature columns
    as None. The ML re-scoring step skips rows that lack features.

    Rows tagged with a ``data_quality_flag`` are excluded so the
    attribution math is computed on real strategy outcomes only.
    Without this, pre-fix artifact rows (CATASTROPHIC_SHORT,
    CORRUPTED_PNL, EXIT_REASON_RECLASSED, PRE_BUG027_WALLET) skew
    per-gate protecting/leaking PnL and bias the recommendations.
    """
    from sqlalchemy import create_engine, text
    engine = create_engine(_normalize_db_url(db_url))
    sql = text("""
        SELECT
            t.id AS trade_id,
            t.timestamp,
            t.ticker,
            t.direction,
            t.conviction,
            t.entry_price,
            t.pnl,
            t.signal_driver,
            t.trading_mode,
            f.obi, f.roc_3, f.roc_5, f.roc_10,
            f.atr_pct, f.spread_pct, f.bid_depth, f.ask_depth,
            f.green_candles_3, f.candle_body_pct, f.volume_ratio,
            f.time_remaining_sec, f.hour_of_day, f.day_of_week,
            f.minutes_to_contract_close, f.quoted_spread_at_entry_bps,
            f.book_thickness_at_offer, f.recent_trade_count_60s
        FROM trades t
        LEFT JOIN trade_features f ON f.trade_id = t.id
        WHERE t.timestamp > NOW() - (:days || ' days')::interval
          AND t.trading_mode = :mode
          AND t.pnl IS NOT NULL
          AND t.data_quality_flag IS NULL
        ORDER BY t.timestamp ASC
    """)
    with engine.connect() as conn:
        result = conn.execute(sql, {"days": window_days, "mode": mode})
        rows = [dict(r._mapping) for r in result]
    return rows


def filter_through_ml(rows: list[dict], model_path: Optional[Path]) -> list[dict]:
    """Drop rows the active ML model would have rejected. Keep rows
    where feature snapshot is missing (no ml_gate decision possible).
    """
    if model_path is None or not model_path.exists():
        print(f"  ML conditional skipped: model artifact not found at {model_path}",
              file=sys.stderr)
        return rows
    try:
        import joblib
        artifact = joblib.load(model_path)
    except Exception as e:
        print(f"  ML conditional skipped: failed to load artifact ({e})",
              file=sys.stderr)
        return rows
    model = artifact["model"]
    features = artifact["features"]
    threshold = artifact.get("threshold", 0.55)

    import numpy as np
    kept = []
    rejected_count = 0
    for r in rows:
        if r.get("obi") is None:
            kept.append(r)
            continue
        feature_vec = np.array([[float(r.get(f) or 0) for f in features]])
        try:
            p_win = float(model.predict_proba(feature_vec)[0][1])
        except Exception:
            kept.append(r)
            continue
        if p_win >= threshold:
            kept.append({**r, "_ml_p_win": round(p_win, 4)})
        else:
            rejected_count += 1
    print(f"  ML conditional: kept {len(kept)}, dropped {rejected_count}")
    return kept


# ─── Per-gate attribution ─────────────────────────────────────────────────

def _conviction_rank(level: str) -> int:
    return {"NONE": 0, "LOW": 1, "NORMAL": 2, "HIGH": 3}.get(
        (level or "").upper(), 0,
    )


def attribute_short_min_price(
    rows: list[dict], current_value: float,
) -> dict:
    """Compute protecting / leaking PnL for the short_min_price gate.

    Protecting: shorts BELOW current_value that lost money in paper.
    Leaking: shorts AT OR ABOVE current_value that the gate currently
             allows (this number tells us "is the gate too aggressive
             on the high-price side?"). For tightening recommendations
             we look at the cohort just above current_value to see if
             raising the floor would protect more PnL than it sacrifices.
    """
    shorts = [r for r in rows if r["direction"] == "short"]
    below_pnl = sum(float(r["pnl"]) for r in shorts
                    if float(r["entry_price"]) < current_value)
    below_count = sum(1 for r in shorts
                      if float(r["entry_price"]) < current_value)
    above_pnl = sum(float(r["pnl"]) for r in shorts
                    if float(r["entry_price"]) >= current_value)
    above_count = sum(1 for r in shorts
                      if float(r["entry_price"]) >= current_value)

    suggested = current_value
    suggested_n = above_count
    suggested_pnl = above_pnl
    candidate_floors = sorted({
        float(r["entry_price"]) for r in shorts
        if float(r["entry_price"]) >= current_value
    })
    for candidate in candidate_floors:
        cohort = [r for r in shorts
                  if current_value <= float(r["entry_price"]) < candidate]
        if len(cohort) < 5:
            continue
        cohort_pnl = sum(float(r["pnl"]) for r in cohort)
        if cohort_pnl < -100.0:
            suggested = candidate
            suggested_n = sum(1 for r in shorts
                              if float(r["entry_price"]) >= candidate)
            suggested_pnl = sum(float(r["pnl"]) for r in shorts
                                if float(r["entry_price"]) >= candidate)

    return {
        "param": "EDGE_LIVE_SHORT_MIN_PRICE",
        "current": current_value,
        "suggested": suggested,
        "protecting_pnl": round(below_pnl, 2),
        "protecting_n": below_count,
        "leaking_pnl": round(above_pnl, 2),
        "leaking_n": above_count,
        "suggested_n_supporting": suggested_n,
        "suggested_pnl_after": round(suggested_pnl, 2),
        "pnl_impact_dollars": round(suggested_pnl - above_pnl, 2),
    }


def attribute_max_entry_price(rows: list[dict], current_value: float) -> dict:
    """Long-side cap. Protecting = expensive longs we currently reject;
    leaking = expensive longs we currently allow (because the cap was
    set higher than they came in)."""
    longs = [r for r in rows if r["direction"] == "long"]
    above_pnl = sum(float(r["pnl"]) for r in longs
                    if float(r["entry_price"]) > current_value)
    above_count = sum(1 for r in longs
                      if float(r["entry_price"]) > current_value)
    below_pnl = sum(float(r["pnl"]) for r in longs
                    if float(r["entry_price"]) <= current_value)
    below_count = sum(1 for r in longs
                      if float(r["entry_price"]) <= current_value)

    suggested = current_value
    suggested_n = below_count
    suggested_pnl = below_pnl
    candidate_caps = sorted({
        float(r["entry_price"]) for r in longs
        if float(r["entry_price"]) <= current_value
    }, reverse=True)
    for candidate in candidate_caps:
        cohort = [r for r in longs
                  if candidate < float(r["entry_price"]) <= current_value]
        if len(cohort) < 5:
            continue
        cohort_pnl = sum(float(r["pnl"]) for r in cohort)
        if cohort_pnl < -100.0:
            suggested = candidate
            suggested_n = sum(1 for r in longs
                              if float(r["entry_price"]) <= candidate)
            suggested_pnl = sum(float(r["pnl"]) for r in longs
                                if float(r["entry_price"]) <= candidate)

    return {
        "param": "EDGE_LIVE_MAX_ENTRY_PRICE",
        "current": current_value,
        "suggested": suggested,
        "protecting_pnl": round(above_pnl, 2),
        "protecting_n": above_count,
        "leaking_pnl": round(below_pnl, 2),
        "leaking_n": below_count,
        "suggested_n_supporting": suggested_n,
        "suggested_pnl_after": round(suggested_pnl, 2),
        "pnl_impact_dollars": round(suggested_pnl - below_pnl, 2),
    }


def attribute_blocked_hours(
    rows: list[dict], current_value: str,
) -> dict:
    """Blocked-hours gate. Suggested = current ∪ hours that lost >$200
    over ≥10 trades. Tightening only — we never suggest dropping hours
    via this attribution path (that would be loosening).
    """
    cur_blocked = _parse_csv_int_set(current_value)
    by_hour = defaultdict(lambda: {"pnl": 0.0, "n": 0})
    for r in rows:
        ts = r["timestamp"]
        hour = ts.hour if hasattr(ts, "hour") else None
        if hour is None:
            continue
        by_hour[hour]["pnl"] += float(r["pnl"])
        by_hour[hour]["n"] += 1

    suggested_set = set(cur_blocked)
    n_supporting = 0
    pnl_blocked = 0.0
    for hour, stats in by_hour.items():
        if hour in cur_blocked:
            continue
        if stats["n"] >= 10 and stats["pnl"] < -200.0:
            suggested_set.add(hour)
            n_supporting += stats["n"]
            pnl_blocked += stats["pnl"]

    suggested = ",".join(str(h) for h in sorted(suggested_set))
    return {
        "param": "EDGE_LIVE_BLOCKED_HOURS_UTC",
        "current": current_value,
        "suggested": suggested,
        "protecting_pnl": round(-pnl_blocked, 2),
        "protecting_n": n_supporting,
        "leaking_pnl": 0.0,
        "leaking_n": 0,
        "suggested_n_supporting": n_supporting,
        "suggested_pnl_after": 0.0,
        "pnl_impact_dollars": round(-pnl_blocked, 2),
    }


def attribute_short_negative_roc(
    rows: list[dict], current_value: float,
) -> dict:
    """Compute protecting / leaking PnL for the ROC-contradiction short veto.

    Uses ``trade_features.roc_5`` (the same value the live filter sees).
    Filter is short-only and gated on conviction == NORMAL. We bucket
    NORMAL shorts by raw roc_5, find candidate thresholds (each unique
    roc_5 value in the dataset), and pick the threshold that protects
    a cohort of >= 5 trades losing more than -$100. Suggested value is
    the most-permissive threshold satisfying that constraint (i.e. the
    *least* negative; we don't want to over-block).

    Returns the same shape as ``attribute_short_min_price`` so the
    Discord report renders uniformly.
    """
    normal_shorts = [
        r for r in rows
        if r["direction"] == "short"
        and (r.get("conviction") or "").upper() == "NORMAL"
        and r.get("roc_5") is not None
    ]
    if not normal_shorts:
        return {
            "param": "EDGE_LIVE_SHORT_BLOCK_NEGATIVE_ROC_THRESHOLD",
            "current": float(current_value),
            "suggested": float(current_value),
            "protecting_pnl": 0.0,
            "protecting_n": 0,
            "leaking_pnl": 0.0,
            "leaking_n": 0,
            "suggested_n_supporting": 0,
            "suggested_pnl_after": 0.0,
            "pnl_impact_dollars": 0.0,
        }

    cur_active = float(current_value) < 0.0
    if cur_active:
        below_pnl = sum(float(r["pnl"]) for r in normal_shorts
                        if float(r["roc_5"]) <= float(current_value))
        below_count = sum(1 for r in normal_shorts
                          if float(r["roc_5"]) <= float(current_value))
        above_pnl = sum(float(r["pnl"]) for r in normal_shorts
                        if float(r["roc_5"]) > float(current_value))
        above_count = sum(1 for r in normal_shorts
                          if float(r["roc_5"]) > float(current_value))
    else:
        below_pnl = 0.0
        below_count = 0
        above_pnl = sum(float(r["pnl"]) for r in normal_shorts)
        above_count = len(normal_shorts)

    suggested = float(current_value)
    suggested_n = above_count
    suggested_pnl = above_pnl
    candidate_thresholds = sorted({
        round(float(r["roc_5"]), 4) for r in normal_shorts
        if float(r["roc_5"]) < (float(current_value) if cur_active else 0.0)
    })
    for candidate in candidate_thresholds:
        if cur_active and candidate >= float(current_value):
            continue
        if cur_active:
            cohort = [
                r for r in normal_shorts
                if candidate < float(r["roc_5"]) <= float(current_value)
            ]
        else:
            cohort = [
                r for r in normal_shorts
                if candidate < float(r["roc_5"]) <= 0.0
            ]
        if len(cohort) < 5:
            continue
        cohort_pnl = sum(float(r["pnl"]) for r in cohort)
        if cohort_pnl < -100.0:
            suggested = candidate
            suggested_n = sum(1 for r in normal_shorts
                              if float(r["roc_5"]) > candidate)
            suggested_pnl = sum(float(r["pnl"]) for r in normal_shorts
                                if float(r["roc_5"]) > candidate)

    return {
        "param": "EDGE_LIVE_SHORT_BLOCK_NEGATIVE_ROC_THRESHOLD",
        "current": float(current_value),
        "suggested": float(suggested),
        "protecting_pnl": round(below_pnl, 2),
        "protecting_n": below_count,
        "leaking_pnl": round(above_pnl, 2),
        "leaking_n": above_count,
        "suggested_n_supporting": suggested_n,
        "suggested_pnl_after": round(suggested_pnl, 2),
        "pnl_impact_dollars": round(suggested_pnl - above_pnl, 2),
    }


def attribute_allowed_drivers(rows: list[dict], current_value: str) -> dict:
    """Driver allowlist. Suggested = current minus drivers that lost
    >$200 over ≥10 trades."""
    cur_allowed = _parse_csv_str_set(current_value)
    by_driver = defaultdict(lambda: {"pnl": 0.0, "n": 0})
    for r in rows:
        d = (r.get("signal_driver") or "").strip()
        if not d or d not in cur_allowed:
            continue
        by_driver[d]["pnl"] += float(r["pnl"])
        by_driver[d]["n"] += 1

    suggested_set = set(cur_allowed)
    n_supporting = 0
    pnl_removed = 0.0
    for driver, stats in by_driver.items():
        if stats["n"] >= 10 and stats["pnl"] < -200.0:
            suggested_set.discard(driver)
            n_supporting += stats["n"]
            pnl_removed += stats["pnl"]

    suggested = ",".join(sorted(suggested_set))
    return {
        "param": "EDGE_LIVE_ALLOWED_DRIVERS",
        "current": current_value,
        "suggested": suggested,
        "protecting_pnl": round(-pnl_removed, 2),
        "protecting_n": n_supporting,
        "leaking_pnl": 0.0,
        "leaking_n": 0,
        "suggested_n_supporting": n_supporting,
        "suggested_pnl_after": 0.0,
        "pnl_impact_dollars": round(-pnl_removed, 2),
    }


# ─── Top-level review ─────────────────────────────────────────────────────

def build_recommendations(
    rows: list[dict], current_config: dict,
) -> list[dict]:
    """Compute one recommendation per tunable param and tag tier."""
    recs = []
    recs.append(attribute_short_min_price(
        rows, float(current_config.get("EDGE_LIVE_SHORT_MIN_PRICE", 40.0))))
    recs.append(attribute_max_entry_price(
        rows, float(current_config.get("EDGE_LIVE_MAX_ENTRY_PRICE", 25.0))))
    recs.append(attribute_blocked_hours(
        rows, current_config.get("EDGE_LIVE_BLOCKED_HOURS_UTC", "")))
    recs.append(attribute_allowed_drivers(
        rows, current_config.get("EDGE_LIVE_ALLOWED_DRIVERS", "OBI,OBI+ROC,ROC")))
    recs.append(attribute_short_negative_roc(
        rows, float(current_config.get(
            "EDGE_LIVE_SHORT_BLOCK_NEGATIVE_ROC_THRESHOLD", -0.05))))
    for rec in recs:
        rec["is_tightening"] = is_tightening_for_param(
            rec["param"], rec["current"], rec["suggested"],
        )
        rec["tier"] = classify_recommendation(
            param=rec["param"],
            current=rec["current"],
            suggested=rec["suggested"],
            n_supporting_trades=rec["suggested_n_supporting"],
            pnl_impact_dollars=rec["pnl_impact_dollars"],
            is_tightening=rec["is_tightening"],
        )
    return recs


def read_current_env_config(env_file: Optional[Path]) -> dict:
    """Load current EDGE_LIVE_* values from a .env file. Falls back to
    EdgeProfileConfig defaults when the file is absent (i.e. running
    locally for tests)."""
    cfg = {}
    if env_file is not None and env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.startswith("EDGE_LIVE_"):
                cfg[k] = v.strip()
    if not cfg:
        try:
            from config.settings import EdgeProfileConfig
            ep = EdgeProfileConfig()
            cfg = {
                "EDGE_LIVE_PROFILE_ENABLED": str(ep.enabled).lower(),
                "EDGE_LIVE_LONG_ONLY": str(ep.long_only).lower(),
                "EDGE_LIVE_SHORT_MIN_PRICE": str(ep.short_min_price),
                "EDGE_LIVE_SHORT_MIN_CONVICTION": ep.short_min_conviction,
                "EDGE_LIVE_BLOCK_LOW_CONVICTION": str(ep.block_low_conviction).lower(),
                "EDGE_LIVE_MAX_ENTRY_PRICE": str(ep.max_entry_price),
                "EDGE_LIVE_AGREEMENT_OVERRIDES_PRICE_CAP": str(ep.agreement_overrides_price_cap).lower(),
                "EDGE_LIVE_ALLOWED_DRIVERS": ep.allowed_drivers,
                "EDGE_LIVE_BLOCKED_HOURS_UTC": ep.blocked_hours_utc,
            }
        except Exception:
            pass
    return cfg


def read_ml_status(model_dir: Path) -> dict:
    """Best-effort ML status snapshot: incumbent precision, last
    promotion outcome, candidate hold reason if any."""
    status: dict[str, Any] = {
        "incumbent_precision": None,
        "incumbent_rows": None,
        "last_promotion": None,
        "last_outcome": None,
        "training_mode": None,
    }
    meta_path = model_dir / "xgb_entry_v1_meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            status["incumbent_precision"] = meta.get("oos_precision")
            status["incumbent_rows"] = meta.get("rows")
            status["training_mode"] = meta.get("training_mode")
        except Exception:
            pass
    log_path = model_dir / ".promotion_log.json"
    if log_path.exists():
        try:
            log = json.loads(log_path.read_text())
            if isinstance(log, list) and log:
                last = log[-1]
                status["last_promotion"] = last.get("timestamp")
                status["last_outcome"] = last.get("outcome")
        except Exception:
            pass
    return status


# ─── Reporting ────────────────────────────────────────────────────────────

def format_markdown(
    *,
    window_days: int,
    mode: str,
    n_trades: int,
    ml_status: dict,
    recommendations: list[dict],
    auto_applied_last_cycle: list[dict],
) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"# Weekly edge_profile + ML co-calibration review",
        f"**Week ending:** {today}",
        f"**Window:** last {window_days} days, mode={mode}, "
        f"{n_trades} trades after ML conditional filter",
        "",
        "## ML status",
        f"- **Incumbent precision:** {ml_status.get('incumbent_precision', 'unknown')}",
        f"- **Incumbent rows:** {ml_status.get('incumbent_rows', 'unknown')}",
        f"- **Training mode:** {ml_status.get('training_mode', 'unknown')}",
        f"- **Last promotion:** {ml_status.get('last_promotion', 'never')}",
        f"- **Last outcome:** {ml_status.get('last_outcome', 'unknown')}",
        "",
    ]
    auto_recs = [r for r in recommendations if r["tier"] == "AUTO_APPLY"]
    manual_recs = [r for r in recommendations if r["tier"] == "MANUAL_ONLY"
                   and r["current"] != r["suggested"]]

    lines.append("## Auto-applied this cycle")
    if auto_applied_last_cycle:
        for c in auto_applied_last_cycle:
            lines.append(
                f"- `{c['param']}`: `{c['old_value']}` → `{c['new_value']}` "
                f"(applied {c['changed_at']})"
            )
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("## Tier 1 candidates for next auto-apply")
    if auto_recs:
        for r in auto_recs:
            lines.append(
                f"- `{r['param']}`: **{r['current']}** → **{r['suggested']}** "
                f"(impact: ${r['pnl_impact_dollars']:+.2f}, n={r['suggested_n_supporting']})"
            )
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("## Manual review required")
    if manual_recs:
        for r in manual_recs:
            why = []
            if not r["is_tightening"]:
                why.append("LOOSENING")
            if r["param"] in KILL_SWITCH_PARAMS:
                why.append("KILL_SWITCH")
            if r["suggested_n_supporting"] < AUTO_APPLY_MIN_TRADES:
                why.append(f"low n={r['suggested_n_supporting']}")
            if abs(r["pnl_impact_dollars"]) < AUTO_APPLY_MIN_PNL_IMPACT:
                why.append(f"low |PnL|=${abs(r['pnl_impact_dollars']):.0f}")
            why_str = ",".join(why) if why else "—"
            lines.append(
                f"- `{r['param']}`: **{r['current']}** → **{r['suggested']}** "
                f"(impact: ${r['pnl_impact_dollars']:+.2f}, n={r['suggested_n_supporting']}, "
                f"reason for manual: {why_str})"
            )
            sed_template = (
                f"  ssh \"$KBTC_DEPLOY_HOST\" \"sed -i 's/^{r['param']}=.*/"
                f"{r['param']}={r['suggested']}/' ~/kbtc/.env\""
            )
            lines.append(sed_template)
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("## Top 'leaking' gates (PnL allowed through)")
    leaking_sorted = sorted(
        recommendations, key=lambda x: x["leaking_pnl"], reverse=True,
    )[:3]
    for r in leaking_sorted:
        lines.append(
            f"- `{r['param']}`: ${r['leaking_pnl']:+.2f} across {r['leaking_n']} trades"
        )
    lines.append("")

    lines.append("## Top 'protecting' gates (PnL rejected)")
    prot_sorted = sorted(
        recommendations, key=lambda x: x["protecting_pnl"], reverse=True,
    )[:3]
    for r in prot_sorted:
        lines.append(
            f"- `{r['param']}`: ${r['protecting_pnl']:+.2f} across {r['protecting_n']} trades"
        )
    lines.append("")
    lines.append(
        "_Operator action: review the manual section. Auto-applied changes are "
        "reversible via `~/kbtc/.env.backup-auto-<ts>`. See "
        "docs/runbooks/edge-profile-review.md._"
    )
    return "\n".join(lines)


def post_to_discord(webhook_url: str, content: str) -> None:
    """Post a markdown body to a Discord webhook.

    Discord returns 403 for the default ``Python-urllib/3.x`` User-Agent,
    so we set a generic ``Mozilla/5.0`` UA the same way curl/the bot's
    httpx-based notifier does (httpx sends ``python-httpx/x.y`` which
    Discord accepts).
    """
    if not webhook_url:
        print("  Discord post skipped: no webhook URL")
        return
    import urllib.request
    body = content
    if len(body) > 1900:
        body = body[:1850] + "\n\n…(truncated; full report in JSON sidecar)"
    payload = json.dumps({"content": body}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "kbtc-edge-review/1.0 (+https://github.com/kbtc-bot)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 204):
                print(f"  Discord post returned {resp.status}", file=sys.stderr)
    except Exception as e:
        print(f"  Discord post failed: {e}", file=sys.stderr)


# ─── CLI ──────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--window-days", type=int, default=14)
    p.add_argument("--mode", default="paper",
                   choices=["paper", "live", "both"])
    p.add_argument("--ml-conditional", action="store_true", default=True)
    p.add_argument("--no-ml-conditional", dest="ml_conditional",
                   action="store_false")
    p.add_argument("--ml-snapshot-path", type=Path, default=None)
    p.add_argument("--db-url", default=os.environ.get(
        "DATABASE_URL",
        "postgresql://kalshi:kalshi_secret@localhost:5432/kbtc",
    ))
    p.add_argument("--env-file", type=Path, default=None,
                   help="Path to .env file with current EDGE_LIVE_* values")
    p.add_argument("--output-dir", type=Path,
                   default=Path("/home/botuser/kbtc/data/edge_review"))
    p.add_argument("--discord-webhook", default=os.environ.get(
        "DISCORD_ATTRIBUTION_WEBHOOK", "",
    ))
    p.add_argument("--post-discord", action="store_true", default=False)
    p.add_argument("--print-only", action="store_true", default=False,
                   help="Compute and print the report; skip Discord and "
                        "skip writing the JSON sidecar")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    print(f"=== edge_profile_review {_utc_stamp()} ===")
    print(f"  window_days={args.window_days}, mode={args.mode}, "
          f"ml_conditional={args.ml_conditional}")

    try:
        rows = load_trades_with_features(
            args.db_url, args.window_days, args.mode,
        )
    except Exception as e:
        print(f"FATAL: failed to load trades: {e}", file=sys.stderr)
        return 1
    print(f"  Loaded {len(rows)} trades from DB")

    if args.ml_conditional:
        model_path = args.ml_snapshot_path or (
            _BACKEND_DIR / "ml" / "models" / "xgb_entry_v1.pkl"
        )
        rows = filter_through_ml(rows, model_path)

    current_cfg = read_current_env_config(args.env_file)
    print(f"  Loaded {len(current_cfg)} EDGE_LIVE_* values")

    recommendations = build_recommendations(rows, current_cfg)
    ml_status = read_ml_status(_BACKEND_DIR / "ml" / "models")

    auto_applied_last_cycle = _fetch_recent_auto_changes(args.db_url)

    md = format_markdown(
        window_days=args.window_days,
        mode=args.mode,
        n_trades=len(rows),
        ml_status=ml_status,
        recommendations=recommendations,
        auto_applied_last_cycle=auto_applied_last_cycle,
    )
    print("\n" + md + "\n")

    if args.print_only:
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    sidecar_path = args.output_dir / f"recommendations_{_utc_stamp()}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": args.window_days,
        "mode": args.mode,
        "ml_conditional": args.ml_conditional,
        "n_trades": len(rows),
        "ml_status": ml_status,
        "current_config": current_cfg,
        "recommendations": recommendations,
    }
    sidecar_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"  Sidecar written: {sidecar_path}")

    md_path = args.output_dir / f"report_{_utc_stamp()}.md"
    md_path.write_text(md)
    print(f"  Report written:  {md_path}")

    if args.post_discord:
        post_to_discord(args.discord_webhook, md)

    return 0


def _fetch_recent_auto_changes(db_url: str) -> list[dict]:
    """Read the last 7 days of auto-applied changes from the change log.
    Returns [] if the table doesn't exist yet."""
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(_normalize_db_url(db_url))
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT changed_at, param, old_value, new_value
                FROM edge_profile_change_log
                WHERE applied_by = 'auto'
                  AND changed_at > NOW() - INTERVAL '7 days'
                ORDER BY changed_at DESC
            """))
            return [dict(r._mapping) for r in result]
    except Exception:
        return []


if __name__ == "__main__":
    sys.exit(main())
