"""Auto-apply Tier 1 (tightening-only) edge_profile recommendations.

Reads the JSON sidecar produced by scripts/edge_profile_review.py and
applies recommendations tagged ``AUTO_APPLY`` to the live env file. Every
mutation is preceded by a backup of the env file and an audit row in
``edge_profile_change_log``. Loosening, kill-switch flips, and low-data
recommendations are NEVER auto-applied — they're left for the operator
to action via the Discord report's manual section.

Safety properties:
  * Master kill switch ``EDGE_LIVE_AUTO_APPLY_ENABLED=false`` (default)
    aborts every apply. Operator must explicitly opt in.
  * Per-param 7-day throttle prevents the same param from being
    auto-changed twice in one week, so a noisy week of weekly reviews
    can't ratchet a value way too far in one direction.
  * The audit row is inserted BEFORE the env mutation. If the DB write
    fails, the env file is not touched. If the env mutation fails after
    the audit row exists, the row is rolled back (DELETE by id).
  * After successful applies, the bot is restarted — but only if
    ``/api/deploy-check`` reports no live position open. Restart with
    a position open could double-fill or cancel orders.
  * Every apply is announced to ``DISCORD_RISK_WEBHOOK`` (NOT attribution)
    with a ready-to-paste rollback command.

Usage:
    python scripts/edge_profile_apply.py \
        --recommendations-json /home/botuser/kbtc/data/edge_review/recommendations_<ts>.json \
        --env-file /home/botuser/kbtc/.env \
        [--dry-run] [--no-restart]

Exit codes:
  0  applied at least one change OR no qualifying changes (success)
  1  master kill switch is OFF (expected when operator hasn't opted in)
  2  fatal error (DB unreachable, malformed input, file IO failure)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent
_BACKEND_DIR = _HERE.parent / "backend"
if not _BACKEND_DIR.exists():
    _BACKEND_DIR = Path("/app")
sys.path.insert(0, str(_BACKEND_DIR))

THROTTLE_DAYS = 7

KILL_SWITCH_PARAMS = frozenset({
    "EDGE_LIVE_PROFILE_ENABLED",
    "EDGE_LIVE_LONG_ONLY",
    "EDGE_LIVE_AUTO_APPLY_ENABLED",
})


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ─── Pure helpers (testable) ──────────────────────────────────────────────

def filter_auto_apply(recommendations: list[dict]) -> list[dict]:
    """Keep only recommendations the review tagged AUTO_APPLY AND that
    pass the apply script's defense-in-depth checks. Even though the
    review script is the source of truth for tier tagging, the apply
    script re-checks the loosening/kill-switch invariants so a bug in
    the review can't accidentally promote a dangerous change."""
    out = []
    for r in recommendations:
        if r.get("tier") != "AUTO_APPLY":
            continue
        if r["param"] in KILL_SWITCH_PARAMS:
            continue
        if not r.get("is_tightening", False):
            continue
        if r.get("current") == r.get("suggested"):
            continue
        out.append(r)
    return out


def is_master_kill_switch_off(env_path: Path) -> bool:
    """Return True when ``EDGE_LIVE_AUTO_APPLY_ENABLED`` is missing or
    set to a falsy value in the env file."""
    if not env_path.exists():
        return True
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("EDGE_LIVE_AUTO_APPLY_ENABLED="):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            return val.lower() not in ("true", "1", "yes", "on")
    return True


def filter_throttled(
    recommendations: list[dict],
    last_change_lookup: dict[str, datetime],
    *,
    now: Optional[datetime] = None,
    throttle_days: int = THROTTLE_DAYS,
) -> tuple[list[dict], list[dict]]:
    """Split recommendations into (allowed, throttled) based on the per-
    param last-change lookup. Throttled when the last auto change was
    within ``throttle_days`` of ``now``."""
    now = now or datetime.now(timezone.utc)
    allowed = []
    throttled = []
    cutoff = now - timedelta(days=throttle_days)
    for rec in recommendations:
        last = last_change_lookup.get(rec["param"])
        if last is None:
            allowed.append(rec)
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last > cutoff:
            throttled.append(rec)
        else:
            allowed.append(rec)
    return allowed, throttled


def build_sed_command(env_file: Path, param: str, new_value: Any) -> str:
    """Build the sed command that overwrites a single env line. Quoted
    safely for the shell."""
    new_str = str(new_value)
    safe_new = new_str.replace("/", r"\/").replace("&", r"\&")
    return f"sed -i 's/^{param}=.*/{param}={safe_new}/' {env_file}"


# ─── DB layer ─────────────────────────────────────────────────────────────

def _normalize_db_url(url: str) -> str:
    """Force psycopg v3 driver (the one installed in the bot container)."""
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    return url


def fetch_last_change_per_param(db_url: str) -> dict[str, datetime]:
    """Returns the most-recent auto change timestamp for every param.
    Empty dict if the table doesn't exist yet (first-ever run)."""
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(_normalize_db_url(db_url))
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT param, MAX(changed_at) AS latest
                FROM edge_profile_change_log
                WHERE applied_by = 'auto'
                GROUP BY param
            """))
            return {r._mapping["param"]: r._mapping["latest"]
                    for r in result}
    except Exception as e:
        print(f"  WARNING: throttle lookup failed ({e}); treating all "
              "params as never-changed", file=sys.stderr)
        return {}


def insert_audit_row(
    db_url: str, param: str, old_value: str, new_value: str,
    recommendation: dict, applied_by: str, notes: Optional[str] = None,
) -> Optional[int]:
    """Insert a row into edge_profile_change_log. Returns the new row id
    or None on failure."""
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(_normalize_db_url(db_url))
        with engine.begin() as conn:
            row = conn.execute(text("""
                INSERT INTO edge_profile_change_log
                    (param, old_value, new_value, recommendation_json,
                     applied_by, notes)
                VALUES (:param, :old, :new, :rec::jsonb, :by, :notes)
                RETURNING id
            """), {
                "param": param, "old": str(old_value), "new": str(new_value),
                "rec": json.dumps(recommendation, default=str),
                "by": applied_by, "notes": notes,
            })
            return int(row.scalar())
    except Exception as e:
        print(f"  ERROR: audit row insert failed for {param}: {e}",
              file=sys.stderr)
        return None


def delete_audit_row(db_url: str, row_id: int) -> bool:
    """Roll back an audit row when the env mutation that followed it
    failed. Append-only normally — the only legitimate use of this is
    the post-audit rollback path."""
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(_normalize_db_url(db_url))
        with engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM edge_profile_change_log WHERE id = :id"
            ), {"id": row_id})
        return True
    except Exception as e:
        print(f"  ERROR: audit row rollback failed for id={row_id}: {e}",
              file=sys.stderr)
        return False


# ─── Env mutation ─────────────────────────────────────────────────────────

def backup_env(env_file: Path) -> Path:
    """Copy env file to a timestamped backup. Returns the backup path."""
    ts = _utc_stamp()
    backup = env_file.with_name(f".env.backup-auto-{ts}")
    shutil.copy2(env_file, backup)
    return backup


def apply_env_change(env_file: Path, param: str, new_value: Any) -> bool:
    """Replace the ``PARAM=...`` line in the env file with the new value.

    Implemented as a pure Python read-modify-write rather than ``sed -i``
    so it behaves identically on macOS (BSD sed) and Linux (GNU sed).
    The Discord post still surfaces the equivalent ``sed`` command from
    ``build_sed_command`` so an operator who wants to roll back manually
    has a copy-pasteable one-liner.

    Returns True on success.
    """
    new_str = str(new_value)
    try:
        text = env_file.read_text()
        lines = text.splitlines()
        replaced = False
        for i, line in enumerate(lines):
            if line.startswith(f"{param}="):
                lines[i] = f"{param}={new_str}"
                replaced = True
                break
        if not replaced:
            lines.append(f"{param}={new_str}")
        env_file.write_text("\n".join(lines) + "\n")
        return True
    except Exception as e:
        print(f"  ERROR: env mutation failed for {param}: {e}",
              file=sys.stderr)
        return False


def restore_env(env_file: Path, backup_path: Path) -> None:
    shutil.copy2(backup_path, env_file)


# ─── Bot restart ──────────────────────────────────────────────────────────

def is_safe_to_restart(deploy_check_url: str) -> tuple[bool, str]:
    """Hit /api/deploy-check; return (safe, reason)."""
    try:
        with urllib.request.urlopen(deploy_check_url, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        return False, f"deploy-check unreachable: {e}"
    safe = bool(data.get("safe_to_deploy", False))
    if safe:
        return True, "safe"
    blockers = data.get("blockers", [])
    return False, f"unsafe: {','.join(str(b) for b in blockers)}"


def restart_bot(compose_files: list[str], cwd: Path) -> bool:
    """Run docker compose restart on the bot service. Return True on
    success. Errors are printed and bubbled up via False so the caller
    can post a CRITICAL Discord alert."""
    cmd = ["docker", "compose"]
    for f in compose_files:
        cmd += ["-f", f]
    cmd += ["restart", "bot"]
    try:
        subprocess.run(cmd, check=True, capture_output=True, cwd=str(cwd))
        return True
    except subprocess.CalledProcessError as e:
        print(f"  ERROR: bot restart failed: {e.stderr.decode(errors='replace')}",
              file=sys.stderr)
        return False
    except FileNotFoundError:
        print("  ERROR: docker not on PATH; cannot restart bot",
              file=sys.stderr)
        return False


# ─── Discord ──────────────────────────────────────────────────────────────

def post_discord(webhook_url: str, content: str) -> None:
    """Post to a Discord webhook. Sets a non-default User-Agent because
    Discord returns 403 for the standard ``Python-urllib/3.x`` UA."""
    if not webhook_url:
        return
    body = content
    if len(body) > 1900:
        body = body[:1850] + "\n…(truncated)"
    payload = json.dumps({"content": body}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "kbtc-edge-apply/1.0 (+https://github.com/kbtc-bot)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 204):
                print(f"  Discord post returned {resp.status}", file=sys.stderr)
    except Exception as e:
        print(f"  Discord post failed: {e}", file=sys.stderr)


def format_discord_announcement(
    *, changes: list[dict], backup_path: Path, restart_status: str,
) -> str:
    if not changes:
        return ""
    lines = [
        f"**edge_profile auto-applied {len(changes)} change(s)**",
        "",
        "```",
    ]
    for c in changes:
        lines.append(
            f"{c['param']}: {c['old']} -> {c['new']} "
            f"(impact ${c.get('pnl_impact_dollars', 0):+.0f}, "
            f"n={c.get('n_supporting', 0)})"
        )
    lines += [
        "```",
        f"Restart: {restart_status}",
        f"Rollback: `cp {backup_path} {backup_path.parent / '.env'}`",
        "Audit: edge_profile_change_log table",
    ]
    return "\n".join(lines)


# ─── Main ─────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--recommendations-json", type=Path, required=True)
    p.add_argument("--env-file", type=Path,
                   default=Path("/home/botuser/kbtc/.env"))
    p.add_argument("--db-url", default=os.environ.get(
        "DATABASE_URL",
        "postgresql://kalshi:kalshi_secret@localhost:5432/kbtc",
    ))
    p.add_argument("--deploy-check-url",
                   default="http://localhost:8000/api/deploy-check")
    p.add_argument("--restart-cwd", type=Path,
                   default=Path("/home/botuser/kbtc"))
    p.add_argument("--compose-file", action="append", default=None,
                   help="Repeatable; defaults to docker-compose.yml + .prod.yml")
    p.add_argument("--discord-webhook", default=os.environ.get(
        "DISCORD_RISK_WEBHOOK", "",
    ))
    p.add_argument("--dry-run", action="store_true", default=False)
    p.add_argument("--no-restart", dest="restart", action="store_false",
                   default=True)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    print(f"=== edge_profile_apply {_utc_stamp()} ===")
    print(f"  recommendations: {args.recommendations_json}")
    print(f"  env_file:        {args.env_file}")
    print(f"  dry_run:         {args.dry_run}")
    print(f"  restart:         {args.restart}")

    if is_master_kill_switch_off(args.env_file):
        print("  Master kill switch OFF (EDGE_LIVE_AUTO_APPLY_ENABLED=false). "
              "No changes will be applied. Exiting cleanly.")
        return 1

    try:
        payload = json.loads(args.recommendations_json.read_text())
    except FileNotFoundError:
        print(f"  FATAL: recommendations file not found: "
              f"{args.recommendations_json}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"  FATAL: recommendations JSON malformed: {e}", file=sys.stderr)
        return 2

    recommendations = payload.get("recommendations", [])
    auto_recs = filter_auto_apply(recommendations)
    print(f"  Auto-apply candidates: {len(auto_recs)}")

    last_change = fetch_last_change_per_param(args.db_url)
    allowed, throttled = filter_throttled(auto_recs, last_change)
    print(f"  After throttle: {len(allowed)} allowed, {len(throttled)} throttled")

    if not allowed:
        print("  No qualifying changes after throttle. Exiting.")
        return 0

    if args.dry_run:
        print("\n  --dry-run: would apply:")
        for r in allowed:
            print(f"    {r['param']}: {r['current']} -> {r['suggested']}")
            print(f"      sed: {build_sed_command(args.env_file, r['param'], r['suggested'])}")
        print("  Throttled (would be skipped):")
        for r in throttled:
            print(f"    {r['param']}: throttled (last change too recent)")
        return 0

    backup_path = backup_env(args.env_file)
    print(f"  Backup written: {backup_path}")

    applied: list[dict] = []
    for rec in allowed:
        param = rec["param"]
        old = rec["current"]
        new = rec["suggested"]
        row_id = insert_audit_row(
            args.db_url, param, old, new, rec, applied_by="auto",
            notes=f"weekly-review {payload.get('generated_at', 'unknown')}",
        )
        if row_id is None:
            print(f"  Skipping {param}: audit row insert failed.")
            continue
        ok = apply_env_change(args.env_file, param, new)
        if not ok:
            print(f"  Rolling back audit row {row_id} for {param} (sed failed).")
            delete_audit_row(args.db_url, row_id)
            continue
        applied.append({
            **rec,
            "old": old, "new": new,
            "sed_cmd": build_sed_command(args.env_file, param, new),
            "n_supporting": rec.get("suggested_n_supporting", 0),
        })
        print(f"  Applied: {param} {old} -> {new}")

    if not applied:
        print("  No changes applied (all audit/sed steps failed).")
        return 2

    restart_status = "skipped"
    if args.restart:
        safe, reason = is_safe_to_restart(args.deploy_check_url)
        if not safe:
            restart_status = f"deferred_position_open ({reason})"
            print(f"  Restart deferred: {reason}")
        else:
            compose_files = args.compose_file or [
                "docker-compose.yml", "docker-compose.prod.yml",
            ]
            ok = restart_bot(compose_files, args.restart_cwd)
            restart_status = "restarted" if ok else "failed"

    msg = format_discord_announcement(
        changes=applied, backup_path=backup_path,
        restart_status=restart_status,
    )
    if msg:
        post_discord(args.discord_webhook, msg)

    if restart_status == "failed":
        print("  CRITICAL: env changed but restart failed. Investigate.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
