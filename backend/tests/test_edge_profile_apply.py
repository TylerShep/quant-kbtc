"""Unit tests for scripts/edge_profile_apply.py.

These tests are intentionally extensive because the apply script is the
only piece of the maintenance system that mutates production state
without operator confirmation. Each safety invariant gets a dedicated
test so a future change that breaks one fails loudly.

Invariants the tests pin:
  * MANUAL_ONLY recommendations are never applied.
  * Loosening recommendations are never applied (defense in depth on
    top of the review's tier tagging).
  * Kill-switch params are never applied.
  * Same param can't auto-change twice within the throttle window.
  * Different params CAN both change in the same cycle.
  * Master kill switch (env file flag) aborts everything.
  * Audit row is inserted BEFORE the env mutation; if the mutation
    fails, the audit row is rolled back.
  * --dry-run mutates neither env nor DB.
  * Restart is deferred (not skipped silently) when /api/deploy-check
    reports a live position open.
  * Discord post includes the rollback path/command.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import edge_profile_apply as epa  # noqa: E402


def _rec(*, param, current, suggested, tier="AUTO_APPLY",
         is_tightening=True, n=100, impact=500.0):
    return {
        "param": param,
        "current": current,
        "suggested": suggested,
        "tier": tier,
        "is_tightening": is_tightening,
        "suggested_n_supporting": n,
        "pnl_impact_dollars": impact,
        "protecting_pnl": impact,
        "leaking_pnl": 0.0,
    }


# ─── filter_auto_apply ────────────────────────────────────────────────────

def test_filter_auto_apply_keeps_only_auto_apply_tier():
    recs = [
        _rec(param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=50,
             tier="AUTO_APPLY"),
        _rec(param="EDGE_LIVE_MAX_ENTRY_PRICE", current=30, suggested=20,
             tier="MANUAL_ONLY"),
    ]
    out = epa.filter_auto_apply(recs)
    assert len(out) == 1
    assert out[0]["param"] == "EDGE_LIVE_SHORT_MIN_PRICE"


def test_filter_auto_apply_blocks_kill_switch_even_if_tier_is_auto():
    """Defense in depth — even if the review mistakenly tags a kill
    switch param as AUTO_APPLY, the apply script still blocks it."""
    recs = [
        _rec(param="EDGE_LIVE_LONG_ONLY", current="false", suggested="true",
             tier="AUTO_APPLY"),
        _rec(param="EDGE_LIVE_PROFILE_ENABLED", current="true", suggested="false",
             tier="AUTO_APPLY"),
        _rec(param="EDGE_LIVE_AUTO_APPLY_ENABLED", current="false",
             suggested="true", tier="AUTO_APPLY"),
    ]
    assert epa.filter_auto_apply(recs) == []


def test_filter_auto_apply_blocks_loosening_even_if_tier_is_auto():
    """Defense in depth — even if the review mistakenly tags a loosening
    change as AUTO_APPLY, the apply script still blocks it."""
    recs = [
        _rec(param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=20,
             tier="AUTO_APPLY", is_tightening=False),
    ]
    assert epa.filter_auto_apply(recs) == []


def test_filter_auto_apply_drops_no_op_changes():
    recs = [
        _rec(param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=40,
             tier="AUTO_APPLY"),
    ]
    assert epa.filter_auto_apply(recs) == []


# ─── Master kill switch ───────────────────────────────────────────────────

def test_master_kill_switch_off_when_missing(tmp_path):
    """Missing flag = OFF (safe default)."""
    env = tmp_path / ".env"
    env.write_text("DATABASE_URL=postgresql://nope\n")
    assert epa.is_master_kill_switch_off(env) is True


def test_master_kill_switch_off_when_false(tmp_path):
    env = tmp_path / ".env"
    env.write_text("EDGE_LIVE_AUTO_APPLY_ENABLED=false\n")
    assert epa.is_master_kill_switch_off(env) is True


def test_master_kill_switch_on_when_true(tmp_path):
    env = tmp_path / ".env"
    env.write_text("EDGE_LIVE_AUTO_APPLY_ENABLED=true\n")
    assert epa.is_master_kill_switch_off(env) is False


def test_master_kill_switch_handles_quoted_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text('EDGE_LIVE_AUTO_APPLY_ENABLED="true"\n')
    assert epa.is_master_kill_switch_off(env) is False


def test_master_kill_switch_off_when_env_file_missing(tmp_path):
    assert epa.is_master_kill_switch_off(tmp_path / "nonexistent") is True


# ─── Throttle ────────────────────────────────────────────────────────────

NOW = datetime(2026, 4, 29, 12, tzinfo=timezone.utc)


def test_throttle_blocks_recent_change_on_same_param():
    recs = [
        _rec(param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=50),
    ]
    last_change = {"EDGE_LIVE_SHORT_MIN_PRICE": NOW - timedelta(days=3)}
    allowed, throttled = epa.filter_throttled(recs, last_change, now=NOW)
    assert allowed == []
    assert len(throttled) == 1


def test_throttle_allows_old_change_on_same_param():
    recs = [
        _rec(param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=50),
    ]
    last_change = {"EDGE_LIVE_SHORT_MIN_PRICE": NOW - timedelta(days=10)}
    allowed, throttled = epa.filter_throttled(recs, last_change, now=NOW)
    assert len(allowed) == 1
    assert throttled == []


def test_throttle_is_per_param_not_global():
    """A throttled SHORT_MIN_PRICE doesn't block an unthrottled
    MAX_ENTRY_PRICE in the same cycle."""
    recs = [
        _rec(param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=50),
        _rec(param="EDGE_LIVE_MAX_ENTRY_PRICE", current=30, suggested=20),
    ]
    last_change = {"EDGE_LIVE_SHORT_MIN_PRICE": NOW - timedelta(days=2)}
    allowed, throttled = epa.filter_throttled(recs, last_change, now=NOW)
    assert len(allowed) == 1
    assert allowed[0]["param"] == "EDGE_LIVE_MAX_ENTRY_PRICE"
    assert len(throttled) == 1
    assert throttled[0]["param"] == "EDGE_LIVE_SHORT_MIN_PRICE"


def test_throttle_handles_naive_datetimes():
    """Some psycopg drivers return naive datetimes — must coerce to UTC
    rather than crash."""
    recs = [
        _rec(param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=50),
    ]
    naive = (NOW - timedelta(days=2)).replace(tzinfo=None)
    last_change = {"EDGE_LIVE_SHORT_MIN_PRICE": naive}
    allowed, throttled = epa.filter_throttled(recs, last_change, now=NOW)
    assert allowed == []
    assert len(throttled) == 1


# ─── build_sed_command ───────────────────────────────────────────────────

def test_build_sed_command_basic():
    cmd = epa.build_sed_command(Path("/etc/.env"), "EDGE_LIVE_SHORT_MIN_PRICE", 50.0)
    assert "sed -i" in cmd
    assert "EDGE_LIVE_SHORT_MIN_PRICE=50.0" in cmd
    assert "/etc/.env" in cmd


def test_build_sed_command_escapes_slashes():
    """Some recommended values may contain forward slashes (e.g. driver
    list with date suffix). Must be escaped to keep sed regex valid."""
    cmd = epa.build_sed_command(Path("/etc/.env"), "EDGE_LIVE_X", "OBI/TIGHT")
    assert "OBI\\/TIGHT" in cmd


# ─── format_discord_announcement ──────────────────────────────────────────

def test_discord_announcement_includes_rollback_command():
    backup = Path("/home/botuser/kbtc/.env.backup-auto-20260429")
    msg = epa.format_discord_announcement(
        changes=[{
            "param": "EDGE_LIVE_SHORT_MIN_PRICE",
            "old": 40, "new": 50,
            "pnl_impact_dollars": 600,
            "n_supporting": 80,
            "sed_cmd": "sed -i ...",
        }],
        backup_path=backup,
        restart_status="restarted",
    )
    assert "EDGE_LIVE_SHORT_MIN_PRICE" in msg
    assert "40 -> 50" in msg
    assert "Rollback" in msg
    assert ".env.backup-auto-20260429" in msg


def test_discord_announcement_empty_when_no_changes():
    msg = epa.format_discord_announcement(
        changes=[], backup_path=Path("/tmp/.env.backup-auto-x"),
        restart_status="skipped",
    )
    assert msg == ""


# ─── End-to-end main: dry-run, audit ordering, restart deferral ──────────

def _write_recommendations(tmp_path: Path, recs: list[dict]) -> Path:
    p = tmp_path / "recommendations.json"
    p.write_text(json.dumps({
        "generated_at": "2026-04-29T05:00:00Z",
        "recommendations": recs,
    }))
    return p


def _write_env(tmp_path: Path, lines: list[str]) -> Path:
    p = tmp_path / ".env"
    p.write_text("\n".join(lines) + "\n")
    return p


def test_main_aborts_when_kill_switch_off(tmp_path, capsys):
    rec_file = _write_recommendations(tmp_path, [
        _rec(param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=50),
    ])
    env_file = _write_env(tmp_path, [
        "EDGE_LIVE_AUTO_APPLY_ENABLED=false",
        "EDGE_LIVE_SHORT_MIN_PRICE=40.0",
    ])
    rc = epa.main([
        "--recommendations-json", str(rec_file),
        "--env-file", str(env_file),
        "--db-url", "postgresql://nope/fake",
        "--no-restart",
    ])
    assert rc == 1
    assert "Master kill switch OFF" in capsys.readouterr().out
    assert "EDGE_LIVE_SHORT_MIN_PRICE=40.0" in env_file.read_text()


def test_main_dry_run_makes_no_changes(tmp_path, capsys):
    rec_file = _write_recommendations(tmp_path, [
        _rec(param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=50),
    ])
    env_file = _write_env(tmp_path, [
        "EDGE_LIVE_AUTO_APPLY_ENABLED=true",
        "EDGE_LIVE_SHORT_MIN_PRICE=40.0",
    ])
    with patch.object(epa, "fetch_last_change_per_param", return_value={}), \
            patch.object(epa, "insert_audit_row") as mock_insert, \
            patch.object(epa, "apply_env_change") as mock_apply:
        rc = epa.main([
            "--recommendations-json", str(rec_file),
            "--env-file", str(env_file),
            "--db-url", "postgresql://nope/fake",
            "--no-restart", "--dry-run",
        ])
    assert rc == 0
    mock_insert.assert_not_called()
    mock_apply.assert_not_called()
    assert "EDGE_LIVE_SHORT_MIN_PRICE=40.0" in env_file.read_text()


def test_main_audit_row_inserted_before_env_mutation(tmp_path):
    """Order matters: insert audit row first, then apply sed. Mock both
    to record call order and assert audit precedes apply."""
    rec_file = _write_recommendations(tmp_path, [
        _rec(param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=50),
    ])
    env_file = _write_env(tmp_path, [
        "EDGE_LIVE_AUTO_APPLY_ENABLED=true",
        "EDGE_LIVE_SHORT_MIN_PRICE=40.0",
    ])
    call_order = []
    with patch.object(epa, "fetch_last_change_per_param", return_value={}), \
            patch.object(epa, "insert_audit_row",
                         side_effect=lambda *a, **kw: (call_order.append("audit"), 99)[1]), \
            patch.object(epa, "apply_env_change",
                         side_effect=lambda *a, **kw: (call_order.append("apply"), True)[1]), \
            patch.object(epa, "is_safe_to_restart",
                         return_value=(False, "dry test")), \
            patch.object(epa, "post_discord"):
        rc = epa.main([
            "--recommendations-json", str(rec_file),
            "--env-file", str(env_file),
            "--db-url", "postgresql://nope/fake",
            "--no-restart",
        ])
    assert rc == 0
    assert call_order == ["audit", "apply"]


def test_main_rolls_back_audit_when_env_mutation_fails(tmp_path):
    rec_file = _write_recommendations(tmp_path, [
        _rec(param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=50),
    ])
    env_file = _write_env(tmp_path, [
        "EDGE_LIVE_AUTO_APPLY_ENABLED=true",
        "EDGE_LIVE_SHORT_MIN_PRICE=40.0",
    ])
    with patch.object(epa, "fetch_last_change_per_param", return_value={}), \
            patch.object(epa, "insert_audit_row", return_value=42), \
            patch.object(epa, "apply_env_change", return_value=False), \
            patch.object(epa, "delete_audit_row") as mock_delete, \
            patch.object(epa, "post_discord"):
        rc = epa.main([
            "--recommendations-json", str(rec_file),
            "--env-file", str(env_file),
            "--db-url", "postgresql://nope/fake",
            "--no-restart",
        ])
    assert rc == 2
    mock_delete.assert_called_once_with("postgresql://nope/fake", 42)


def test_main_skips_change_when_audit_insert_fails(tmp_path):
    rec_file = _write_recommendations(tmp_path, [
        _rec(param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=50),
    ])
    env_file = _write_env(tmp_path, [
        "EDGE_LIVE_AUTO_APPLY_ENABLED=true",
        "EDGE_LIVE_SHORT_MIN_PRICE=40.0",
    ])
    with patch.object(epa, "fetch_last_change_per_param", return_value={}), \
            patch.object(epa, "insert_audit_row", return_value=None), \
            patch.object(epa, "apply_env_change") as mock_apply, \
            patch.object(epa, "post_discord"):
        rc = epa.main([
            "--recommendations-json", str(rec_file),
            "--env-file", str(env_file),
            "--db-url", "postgresql://nope/fake",
            "--no-restart",
        ])
    assert rc == 2
    mock_apply.assert_not_called()


def test_main_defers_restart_when_position_open(tmp_path):
    """Restart deferral path: env still mutates, audit row still inserts,
    but docker restart never runs and the Discord post says 'deferred'."""
    rec_file = _write_recommendations(tmp_path, [
        _rec(param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=50),
    ])
    env_file = _write_env(tmp_path, [
        "EDGE_LIVE_AUTO_APPLY_ENABLED=true",
        "EDGE_LIVE_SHORT_MIN_PRICE=40.0",
    ])
    posted = {}
    def _capture_post(url, content):
        posted["content"] = content
    with patch.object(epa, "fetch_last_change_per_param", return_value={}), \
            patch.object(epa, "insert_audit_row", return_value=42), \
            patch.object(epa, "apply_env_change", return_value=True), \
            patch.object(epa, "is_safe_to_restart",
                         return_value=(False, "live position open")), \
            patch.object(epa, "restart_bot") as mock_restart, \
            patch.object(epa, "post_discord", side_effect=_capture_post):
        rc = epa.main([
            "--recommendations-json", str(rec_file),
            "--env-file", str(env_file),
            "--db-url", "postgresql://nope/fake",
        ])
    assert rc == 0
    mock_restart.assert_not_called()
    assert "deferred_position_open" in posted["content"]


def test_main_restarts_when_safe(tmp_path):
    rec_file = _write_recommendations(tmp_path, [
        _rec(param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=50),
    ])
    env_file = _write_env(tmp_path, [
        "EDGE_LIVE_AUTO_APPLY_ENABLED=true",
        "EDGE_LIVE_SHORT_MIN_PRICE=40.0",
    ])
    posted = {}
    def _capture_post(url, content):
        posted["content"] = content
    with patch.object(epa, "fetch_last_change_per_param", return_value={}), \
            patch.object(epa, "insert_audit_row", return_value=42), \
            patch.object(epa, "apply_env_change", return_value=True), \
            patch.object(epa, "is_safe_to_restart",
                         return_value=(True, "safe")), \
            patch.object(epa, "restart_bot", return_value=True), \
            patch.object(epa, "post_discord", side_effect=_capture_post):
        rc = epa.main([
            "--recommendations-json", str(rec_file),
            "--env-file", str(env_file),
            "--db-url", "postgresql://nope/fake",
        ])
    assert rc == 0
    assert "Restart: restarted" in posted["content"]


def test_main_returns_2_when_restart_fails(tmp_path):
    """env was mutated but restart died — operator must investigate.
    Non-zero exit ensures the cron line surfaces the failure."""
    rec_file = _write_recommendations(tmp_path, [
        _rec(param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=50),
    ])
    env_file = _write_env(tmp_path, [
        "EDGE_LIVE_AUTO_APPLY_ENABLED=true",
        "EDGE_LIVE_SHORT_MIN_PRICE=40.0",
    ])
    with patch.object(epa, "fetch_last_change_per_param", return_value={}), \
            patch.object(epa, "insert_audit_row", return_value=42), \
            patch.object(epa, "apply_env_change", return_value=True), \
            patch.object(epa, "is_safe_to_restart",
                         return_value=(True, "safe")), \
            patch.object(epa, "restart_bot", return_value=False), \
            patch.object(epa, "post_discord"):
        rc = epa.main([
            "--recommendations-json", str(rec_file),
            "--env-file", str(env_file),
            "--db-url", "postgresql://nope/fake",
        ])
    assert rc == 2


def test_main_throttled_recommendations_dont_apply(tmp_path):
    rec_file = _write_recommendations(tmp_path, [
        _rec(param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=50),
    ])
    env_file = _write_env(tmp_path, [
        "EDGE_LIVE_AUTO_APPLY_ENABLED=true",
        "EDGE_LIVE_SHORT_MIN_PRICE=40.0",
    ])
    last_change = {"EDGE_LIVE_SHORT_MIN_PRICE": NOW - timedelta(days=2)}
    with patch.object(epa, "fetch_last_change_per_param",
                      return_value=last_change), \
            patch.object(epa, "insert_audit_row") as mock_insert, \
            patch.object(epa, "apply_env_change") as mock_apply:
        rc = epa.main([
            "--recommendations-json", str(rec_file),
            "--env-file", str(env_file),
            "--db-url", "postgresql://nope/fake",
            "--no-restart",
        ])
    assert rc == 0
    mock_insert.assert_not_called()
    mock_apply.assert_not_called()


def test_main_loosening_recommendation_is_dropped_silently(tmp_path):
    """Even with the kill switch on and no throttle, a loosening rec
    must never apply. This is the test that backs the 'Loosening
    NEVER auto-applies' invariant in the rule docs."""
    rec_file = _write_recommendations(tmp_path, [
        _rec(param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=20,
             tier="AUTO_APPLY", is_tightening=False),
    ])
    env_file = _write_env(tmp_path, [
        "EDGE_LIVE_AUTO_APPLY_ENABLED=true",
        "EDGE_LIVE_SHORT_MIN_PRICE=40.0",
    ])
    with patch.object(epa, "fetch_last_change_per_param", return_value={}), \
            patch.object(epa, "insert_audit_row") as mock_insert, \
            patch.object(epa, "apply_env_change") as mock_apply:
        rc = epa.main([
            "--recommendations-json", str(rec_file),
            "--env-file", str(env_file),
            "--db-url", "postgresql://nope/fake",
            "--no-restart",
        ])
    assert rc == 0
    mock_insert.assert_not_called()
    mock_apply.assert_not_called()


def test_main_kill_switch_param_never_auto_applies(tmp_path):
    rec_file = _write_recommendations(tmp_path, [
        _rec(param="EDGE_LIVE_LONG_ONLY", current="false", suggested="true",
             tier="AUTO_APPLY"),
    ])
    env_file = _write_env(tmp_path, [
        "EDGE_LIVE_AUTO_APPLY_ENABLED=true",
        "EDGE_LIVE_LONG_ONLY=false",
    ])
    with patch.object(epa, "fetch_last_change_per_param", return_value={}), \
            patch.object(epa, "insert_audit_row") as mock_insert, \
            patch.object(epa, "apply_env_change") as mock_apply:
        rc = epa.main([
            "--recommendations-json", str(rec_file),
            "--env-file", str(env_file),
            "--db-url", "postgresql://nope/fake",
            "--no-restart",
        ])
    assert rc == 0
    mock_insert.assert_not_called()
    mock_apply.assert_not_called()


def test_main_handles_malformed_recommendations_json(tmp_path):
    rec_file = tmp_path / "bad.json"
    rec_file.write_text("{ this is not valid json")
    env_file = _write_env(tmp_path, [
        "EDGE_LIVE_AUTO_APPLY_ENABLED=true",
    ])
    rc = epa.main([
        "--recommendations-json", str(rec_file),
        "--env-file", str(env_file),
        "--db-url", "postgresql://nope/fake",
        "--no-restart",
    ])
    assert rc == 2


def test_main_handles_missing_recommendations_file(tmp_path):
    env_file = _write_env(tmp_path, [
        "EDGE_LIVE_AUTO_APPLY_ENABLED=true",
    ])
    rc = epa.main([
        "--recommendations-json", str(tmp_path / "nonexistent.json"),
        "--env-file", str(env_file),
        "--db-url", "postgresql://nope/fake",
        "--no-restart",
    ])
    assert rc == 2


def test_main_applies_multiple_qualifying_recommendations(tmp_path):
    """Two qualifying recs in same cycle should both fire (per-param
    throttle is per-param, not global)."""
    rec_file = _write_recommendations(tmp_path, [
        _rec(param="EDGE_LIVE_SHORT_MIN_PRICE", current=40, suggested=50),
        _rec(param="EDGE_LIVE_MAX_ENTRY_PRICE", current=30, suggested=20),
    ])
    env_file = _write_env(tmp_path, [
        "EDGE_LIVE_AUTO_APPLY_ENABLED=true",
        "EDGE_LIVE_SHORT_MIN_PRICE=40.0",
        "EDGE_LIVE_MAX_ENTRY_PRICE=30.0",
    ])
    audit_calls = []
    apply_calls = []
    with patch.object(epa, "fetch_last_change_per_param", return_value={}), \
            patch.object(epa, "insert_audit_row",
                         side_effect=lambda *a, **kw: (audit_calls.append(a[1]), 1)[1]), \
            patch.object(epa, "apply_env_change",
                         side_effect=lambda *a, **kw: (apply_calls.append(a[1]), True)[1]), \
            patch.object(epa, "is_safe_to_restart",
                         return_value=(True, "safe")), \
            patch.object(epa, "restart_bot", return_value=True), \
            patch.object(epa, "post_discord"):
        rc = epa.main([
            "--recommendations-json", str(rec_file),
            "--env-file", str(env_file),
            "--db-url", "postgresql://nope/fake",
        ])
    assert rc == 0
    assert audit_calls == ["EDGE_LIVE_SHORT_MIN_PRICE", "EDGE_LIVE_MAX_ENTRY_PRICE"]
    assert apply_calls == ["EDGE_LIVE_SHORT_MIN_PRICE", "EDGE_LIVE_MAX_ENTRY_PRICE"]


# ─── backup_env / apply_env_change real I/O ──────────────────────────────

def test_backup_env_creates_file_with_distinct_name(tmp_path):
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    backup = epa.backup_env(env)
    assert backup.exists()
    assert backup != env
    assert backup.read_text() == env.read_text()
    assert ".env.backup-auto-" in backup.name


def test_apply_env_change_actually_modifies_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("EDGE_LIVE_SHORT_MIN_PRICE=40.0\nOTHER=keep\n")
    ok = epa.apply_env_change(env, "EDGE_LIVE_SHORT_MIN_PRICE", 50.0)
    assert ok is True
    text = env.read_text()
    assert "EDGE_LIVE_SHORT_MIN_PRICE=50.0" in text
    assert "OTHER=keep" in text
