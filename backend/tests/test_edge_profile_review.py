"""Unit tests for scripts/edge_profile_review.py.

Imports are loaded by inserting the scripts directory on sys.path so we
don't have to convert the script into a package. The pure-logic functions
(``classify_recommendation``, ``is_tightening_for_param``, the per-gate
attribution helpers) need no DB or model — they're tested directly with
synthetic dictionaries.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import edge_profile_review as epr  # noqa: E402


# ─── classify_recommendation ──────────────────────────────────────────────

def test_classify_kill_switch_always_manual():
    """EDGE_LIVE_LONG_ONLY flips never auto-apply, regardless of evidence."""
    tier = epr.classify_recommendation(
        param="EDGE_LIVE_LONG_ONLY", current="false", suggested="true",
        n_supporting_trades=500, pnl_impact_dollars=10_000,
        is_tightening=True,
    )
    assert tier == "MANUAL_ONLY"


def test_classify_loosening_never_auto_apply():
    """Defense in depth — even with tons of evidence, loosening is manual."""
    tier = epr.classify_recommendation(
        param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=20,
        n_supporting_trades=500, pnl_impact_dollars=10_000,
        is_tightening=False,
    )
    assert tier == "MANUAL_ONLY"


def test_classify_no_change_is_manual():
    """A no-op suggestion should never be 'auto-applied'."""
    tier = epr.classify_recommendation(
        param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=40,
        n_supporting_trades=100, pnl_impact_dollars=500,
        is_tightening=True,
    )
    assert tier == "MANUAL_ONLY"


def test_classify_below_min_trades_is_manual():
    tier = epr.classify_recommendation(
        param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=50,
        n_supporting_trades=10, pnl_impact_dollars=500,
        is_tightening=True,
    )
    assert tier == "MANUAL_ONLY"


def test_classify_below_min_pnl_impact_is_manual():
    tier = epr.classify_recommendation(
        param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=50,
        n_supporting_trades=100, pnl_impact_dollars=50,
        is_tightening=True,
    )
    assert tier == "MANUAL_ONLY"


def test_classify_qualifying_tightening_is_auto_apply():
    tier = epr.classify_recommendation(
        param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=50,
        n_supporting_trades=80, pnl_impact_dollars=600,
        is_tightening=True,
    )
    assert tier == "AUTO_APPLY"


# ─── is_tightening_for_param ──────────────────────────────────────────────

def test_tightening_short_min_price_higher_is_tighter():
    assert epr.is_tightening_for_param(
        "EDGE_LIVE_SHORT_MIN_PRICE", 40, 50,
    ) is True
    assert epr.is_tightening_for_param(
        "EDGE_LIVE_SHORT_MIN_PRICE", 40, 30,
    ) is False
    assert epr.is_tightening_for_param(
        "EDGE_LIVE_SHORT_MIN_PRICE", 40, 40,
    ) is False


def test_tightening_max_entry_price_lower_is_tighter():
    assert epr.is_tightening_for_param(
        "EDGE_LIVE_MAX_ENTRY_PRICE", 30, 25,
    ) is True
    assert epr.is_tightening_for_param(
        "EDGE_LIVE_MAX_ENTRY_PRICE", 30, 50,
    ) is False


def test_tightening_short_min_conviction_uses_ladder():
    assert epr.is_tightening_for_param(
        "EDGE_LIVE_SHORT_MIN_CONVICTION", "NORMAL", "HIGH",
    ) is True
    assert epr.is_tightening_for_param(
        "EDGE_LIVE_SHORT_MIN_CONVICTION", "HIGH", "NORMAL",
    ) is False


def test_tightening_blocked_hours_superset_is_tighter():
    assert epr.is_tightening_for_param(
        "EDGE_LIVE_BLOCKED_HOURS_UTC", "0,1,2", "0,1,2,3,4",
    ) is True
    assert epr.is_tightening_for_param(
        "EDGE_LIVE_BLOCKED_HOURS_UTC", "0,1,2,3,4", "0,1,2",
    ) is False
    assert epr.is_tightening_for_param(
        "EDGE_LIVE_BLOCKED_HOURS_UTC", "", "0,1",
    ) is True
    assert epr.is_tightening_for_param(
        "EDGE_LIVE_BLOCKED_HOURS_UTC", "0,1", "",
    ) is False


def test_tightening_allowed_drivers_subset_is_tighter():
    """Removing drivers from the allowlist is more restrictive."""
    assert epr.is_tightening_for_param(
        "EDGE_LIVE_ALLOWED_DRIVERS", "OBI,OBI+ROC,ROC", "OBI,OBI+ROC",
    ) is True
    assert epr.is_tightening_for_param(
        "EDGE_LIVE_ALLOWED_DRIVERS", "OBI,OBI+ROC", "OBI,OBI+ROC,ROC",
    ) is False


def test_tightening_unknown_param_returns_false():
    """Defensive: unrecognized params are never classified as tightening,
    so they fall through to MANUAL_ONLY in the tier logic."""
    assert epr.is_tightening_for_param(
        "EDGE_LIVE_FOO_BAR", "a", "b",
    ) is False


# ─── attribute_short_min_price ────────────────────────────────────────────

def _make_short(price: float, pnl: float, ts: datetime = None):
    return {
        "trade_id": id((price, pnl)),
        "timestamp": ts or datetime(2026, 4, 20, 12, tzinfo=timezone.utc),
        "ticker": "TEST", "direction": "short", "conviction": "NORMAL",
        "entry_price": price, "pnl": pnl, "signal_driver": "OBI",
        "trading_mode": "paper", "obi": 0.5,
    }


def _make_long(price: float, pnl: float, ts: datetime = None):
    r = _make_short(price, pnl, ts)
    r["direction"] = "long"
    return r


def test_attribute_short_min_price_protecting_vs_leaking():
    """Cohort below current $40: 5 losers totaling -$1000 (protecting).
    Cohort at/above $40: 3 winners totaling +$300 (leaking)."""
    rows = [
        _make_short(30, -200), _make_short(30, -250),
        _make_short(35, -200), _make_short(20, -150), _make_short(25, -200),
        _make_short(50, 100), _make_short(60, 100), _make_short(70, 100),
    ]
    out = epr.attribute_short_min_price(rows, 40.0)
    assert out["protecting_n"] == 5
    assert out["protecting_pnl"] == -1000.0
    assert out["leaking_n"] == 3
    assert out["leaking_pnl"] == 300.0


def test_attribute_short_min_price_suggests_tightening_when_loss_above_floor():
    """Cohort just above current $40 shows -$500 loss → suggested floor
    should rise to remove that cohort."""
    rows = (
        [_make_short(p, -100) for p in [40, 41, 42, 43, 44, 45]] +
        [_make_short(p, 100) for p in [55, 60, 65, 70, 75]]
    )
    out = epr.attribute_short_min_price(rows, 40.0)
    assert out["suggested"] > 40.0


def test_attribute_short_min_price_keeps_current_when_above_floor_profitable():
    """Above-floor cohort is profitable → no tightening suggested."""
    rows = (
        [_make_short(p, -100) for p in [25, 30, 32, 35]] +
        [_make_short(p, 200) for p in [45, 50, 55, 60, 65]]
    )
    out = epr.attribute_short_min_price(rows, 40.0)
    assert out["suggested"] == 40.0


# ─── attribute_blocked_hours ──────────────────────────────────────────────

def test_attribute_blocked_hours_adds_losing_hour():
    """Hour 13 has 12 trades, $-500 net → should be added to blocklist."""
    base = datetime(2026, 4, 20, 13, tzinfo=timezone.utc)
    rows = [_make_long(20, -45, base) for _ in range(12)]
    rows += [_make_long(20, 200, base.replace(hour=10)) for _ in range(15)]
    out = epr.attribute_blocked_hours(rows, "")
    assert "13" in out["suggested"]
    assert "10" not in out["suggested"]


def test_attribute_blocked_hours_does_not_add_low_n_loser():
    """Hour 13 has 5 trades, $-500 net → too few trades, don't block."""
    base = datetime(2026, 4, 20, 13, tzinfo=timezone.utc)
    rows = [_make_long(20, -100, base) for _ in range(5)]
    rows += [_make_long(20, 200, base.replace(hour=10)) for _ in range(15)]
    out = epr.attribute_blocked_hours(rows, "")
    assert "13" not in out["suggested"]


def test_attribute_blocked_hours_preserves_current_blocks():
    """Already-blocked hours stay blocked even if not in the data."""
    rows = [_make_long(20, 100, datetime(2026, 4, 20, 10, tzinfo=timezone.utc))
            for _ in range(15)]
    out = epr.attribute_blocked_hours(rows, "0,1")
    suggested_hours = set(out["suggested"].split(","))
    assert "0" in suggested_hours
    assert "1" in suggested_hours


# ─── attribute_allowed_drivers ────────────────────────────────────────────

def test_attribute_allowed_drivers_removes_losing_driver():
    rows = []
    for _ in range(15):
        r = _make_long(20, -100)
        r["signal_driver"] = "ROC"
        rows.append(r)
    for _ in range(15):
        r = _make_long(20, 200)
        r["signal_driver"] = "OBI"
        rows.append(r)
    out = epr.attribute_allowed_drivers(rows, "OBI,OBI+ROC,ROC")
    assert "ROC" not in set(out["suggested"].split(","))
    assert "OBI" in out["suggested"]


def test_attribute_allowed_drivers_keeps_profitable_driver():
    rows = [
        {**_make_long(20, 100), "signal_driver": "ROC"} for _ in range(15)
    ]
    out = epr.attribute_allowed_drivers(rows, "OBI,OBI+ROC,ROC")
    assert "ROC" in set(out["suggested"].split(","))


# ─── build_recommendations end-to-end on a small fixture ──────────────────

def test_build_recommendations_emits_one_per_param():
    """Every tunable param must show up in the recommendations list, with
    a tier tag, even when the suggestion equals the current value."""
    rows = [_make_long(20, 100), _make_short(50, 100)]
    cfg = {
        "EDGE_LIVE_SHORT_MIN_PRICE": "40.0",
        "EDGE_LIVE_MAX_ENTRY_PRICE": "25.0",
        "EDGE_LIVE_BLOCKED_HOURS_UTC": "",
        "EDGE_LIVE_ALLOWED_DRIVERS": "OBI,OBI+ROC,ROC",
    }
    recs = epr.build_recommendations(rows, cfg)
    params = {r["param"] for r in recs}
    assert "EDGE_LIVE_SHORT_MIN_PRICE" in params
    assert "EDGE_LIVE_MAX_ENTRY_PRICE" in params
    assert "EDGE_LIVE_BLOCKED_HOURS_UTC" in params
    assert "EDGE_LIVE_ALLOWED_DRIVERS" in params
    for r in recs:
        assert "tier" in r
        assert r["tier"] in ("AUTO_APPLY", "MANUAL_ONLY")


def test_build_recommendations_tags_qualifying_tightening_as_auto_apply():
    """Synthesise a clear-tightening recommendation and assert tier.

    Need ≥30 supporting trades AFTER the floor moves and >$200 |PnL|
    impact for AUTO_APPLY tier. Generate enough winning trades above
    the new floor that suggested_n_supporting >= 30.
    """
    base = datetime(2026, 4, 20, 12, tzinfo=timezone.utc)
    rows = (
        [_make_short(p, -100, base) for p in [40, 41, 42, 43, 44]] * 8 +
        [_make_short(p, 100, base)
         for p in [50, 51, 52, 55, 60, 65, 70, 75]] * 8
    )
    cfg = {
        "EDGE_LIVE_SHORT_MIN_PRICE": "40.0",
        "EDGE_LIVE_MAX_ENTRY_PRICE": "25.0",
        "EDGE_LIVE_BLOCKED_HOURS_UTC": "",
        "EDGE_LIVE_ALLOWED_DRIVERS": "OBI,OBI+ROC,ROC",
    }
    recs = epr.build_recommendations(rows, cfg)
    short_rec = next(r for r in recs if r["param"] == "EDGE_LIVE_SHORT_MIN_PRICE")
    assert short_rec["suggested"] > 40.0
    assert short_rec["is_tightening"] is True
    assert short_rec["suggested_n_supporting"] >= epr.AUTO_APPLY_MIN_TRADES
    assert abs(short_rec["pnl_impact_dollars"]) >= epr.AUTO_APPLY_MIN_PNL_IMPACT
    assert short_rec["tier"] == "AUTO_APPLY"


# ─── format_markdown sanity ───────────────────────────────────────────────

def test_format_markdown_includes_all_sections():
    md = epr.format_markdown(
        window_days=14, mode="paper", n_trades=100,
        ml_status={"incumbent_precision": 0.62, "last_promotion": "2026-04-26",
                   "last_outcome": "PROMOTED", "training_mode": "both",
                   "incumbent_rows": 500},
        recommendations=[],
        auto_applied_last_cycle=[],
    )
    for section in ("ML status", "Auto-applied this cycle",
                    "Tier 1 candidates", "Manual review required",
                    "leaking", "protecting"):
        assert section in md, f"Missing section: {section}"


def test_format_markdown_lists_auto_applied_changes():
    md = epr.format_markdown(
        window_days=14, mode="paper", n_trades=100,
        ml_status={},
        recommendations=[],
        auto_applied_last_cycle=[
            {"param": "EDGE_LIVE_SHORT_MIN_PRICE", "old_value": "40.0",
             "new_value": "50.0", "changed_at": "2026-04-22T05:30:00Z"},
        ],
    )
    assert "EDGE_LIVE_SHORT_MIN_PRICE" in md
    assert "40.0" in md and "50.0" in md


def test_format_markdown_includes_sed_template_for_manual_recs():
    md = epr.format_markdown(
        window_days=14, mode="paper", n_trades=100,
        ml_status={},
        recommendations=[{
            "param": "EDGE_LIVE_LONG_ONLY",
            "current": "false", "suggested": "true",
            "is_tightening": True,
            "protecting_pnl": 500, "protecting_n": 30,
            "leaking_pnl": 0, "leaking_n": 0,
            "suggested_n_supporting": 30,
            "pnl_impact_dollars": 500,
            "tier": "MANUAL_ONLY",
        }],
        auto_applied_last_cycle=[],
    )
    assert "ssh" in md and "sed" in md and "EDGE_LIVE_LONG_ONLY=true" in md


# ─── env file parsing ────────────────────────────────────────────────────

def test_read_current_env_config_parses_only_edge_live_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "DATABASE_URL=postgresql://nope\n"
        "EDGE_LIVE_LONG_ONLY=false\n"
        "EDGE_LIVE_SHORT_MIN_PRICE=40.0\n"
        "# comment\n"
        "OTHER_KEY=ignore_me\n"
    )
    cfg = epr.read_current_env_config(env)
    assert cfg == {
        "EDGE_LIVE_LONG_ONLY": "false",
        "EDGE_LIVE_SHORT_MIN_PRICE": "40.0",
    }


def test_read_current_env_config_falls_back_to_defaults(tmp_path):
    """No env file → fall back to EdgeProfileConfig defaults so a local
    test run doesn't error."""
    cfg = epr.read_current_env_config(None)
    assert "EDGE_LIVE_SHORT_MIN_PRICE" in cfg


# ─── Helper parsers ──────────────────────────────────────────────────────

def test_parse_csv_int_set_handles_blanks_and_whitespace():
    assert epr._parse_csv_int_set("0,1, 2 ,") == {0, 1, 2}
    assert epr._parse_csv_int_set("") == set()
    assert epr._parse_csv_int_set(None) == set()


def test_parse_csv_str_set_handles_blanks_and_whitespace():
    assert epr._parse_csv_str_set("OBI, ROC ,") == {"OBI", "ROC"}
    assert epr._parse_csv_str_set("") == set()


def test_to_bool_accepts_common_truthy_strings():
    assert epr._to_bool("true") is True
    assert epr._to_bool("TRUE") is True
    assert epr._to_bool("1") is True
    assert epr._to_bool("yes") is True
    assert epr._to_bool("false") is False
    assert epr._to_bool("0") is False
    assert epr._to_bool(True) is True
    assert epr._to_bool(False) is False
