#!/usr/bin/env bash
set -euo pipefail

# Automated canary validation report — runs on the droplet via cron.
# Posts results to Discord errors webhook. No SSH needed.
#
# Cron entry (daily at 09:00 UTC):
#   0 9 * * * /home/botuser/kbtc/scripts/canary_report_cron.sh >> /home/botuser/kbtc/logs/canary_report.log 2>&1

PROJECT_DIR="/home/botuser/kbtc"
CANARY_DB="kbtc-db-canary"
DB_NAME="kbtc_canary"
DB_USER="kalshi"

# Load Discord webhook from prod .env
DISCORD_WEBHOOK=""
if [[ -f "${PROJECT_DIR}/.env" ]]; then
  DISCORD_WEBHOOK=$(grep -E '^DISCORD_ERRORS_WEBHOOK=' "${PROJECT_DIR}/.env" | cut -d= -f2-)
fi

PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0
REPORT=""

add_line() { REPORT+="$1"$'\n'; }
pass() { add_line "  ✅ $1"; ((PASS_COUNT++)) || true; }
fail() { add_line "  ❌ $1"; ((FAIL_COUNT++)) || true; }
warn() { add_line "  ⚠️  $1"; ((WARN_COUNT++)) || true; }

run_sql() {
  docker exec "${CANARY_DB}" psql -U "${DB_USER}" -d "${DB_NAME}" -t -A -c "$1" 2>/dev/null
}

add_line "**CANARY ORPHAN SAFETY REPORT**"
add_line "$(date -u '+%Y-%m-%d %H:%M UTC')"
add_line ""

# ── Gate 1: Canary health ────────────────────────────────────────────────────
API_OK=$(curl -sf http://localhost:8100/api/status >/dev/null 2>&1 && echo yes || echo no)
if [[ "$API_OK" == "yes" ]]; then
  pass "Canary API reachable"
else
  fail "Canary API UNREACHABLE"
fi

# ── Gate 2: No orphan-settled trades ─────────────────────────────────────────
ORPHAN_SETTLED=$(run_sql "SELECT COUNT(*) FROM trades WHERE exit_reason = 'ORPHAN_SETTLED' AND trading_mode = 'live';" || echo "-1")
if [[ "$ORPHAN_SETTLED" == "0" ]]; then
  pass "Zero ORPHAN_SETTLED trades"
elif [[ "$ORPHAN_SETTLED" == "-1" ]]; then
  fail "Cannot query canary DB"
else
  fail "${ORPHAN_SETTLED} ORPHAN_SETTLED trades found"
fi

# ── Gate 3: No oversized orphans ─────────────────────────────────────────────
OVERSIZED=$(run_sql "SELECT COUNT(*) FROM errored_trades WHERE error_reason LIKE '%oversized_orphan%';" || echo "-1")
if [[ "$OVERSIZED" == "0" ]]; then
  pass "Zero oversized orphans"
elif [[ "$OVERSIZED" == "-1" ]]; then
  warn "Cannot check errored_trades"
else
  fail "${OVERSIZED} oversized orphan events"
fi

# ── Gate 4: No DESYNC state ──────────────────────────────────────────────────
PM_STATE=$(curl -sf http://localhost:8100/api/status 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('live', {}).get('state', 'UNKNOWN'))
except: print('UNKNOWN')
" 2>/dev/null || echo "UNKNOWN")

if [[ "$PM_STATE" == "FLAT" || "$PM_STATE" == "OPEN" ]]; then
  pass "State: ${PM_STATE}"
elif [[ "$PM_STATE" == "DESYNC" ]]; then
  fail "DESYNC state detected"
else
  warn "State unknown: ${PM_STATE}"
fi

# ── Gate 5: No duplicate trades ──────────────────────────────────────────────
DUPES=$(run_sql "
  SELECT COUNT(*) FROM (
    SELECT ticker, DATE_TRUNC('minute', timestamp) as minute_bucket
    FROM trades WHERE trading_mode = 'live'
    GROUP BY ticker, DATE_TRUNC('minute', timestamp)
    HAVING COUNT(*) > 1
  ) dupes;
" || echo "-1")

if [[ "$DUPES" == "0" ]]; then
  pass "Zero duplicate trades"
elif [[ "$DUPES" == "-1" ]]; then
  warn "Cannot check duplicates"
else
  fail "${DUPES} duplicate trade buckets"
fi

# ── Gate 6: Activity ─────────────────────────────────────────────────────────
TRADE_COUNT=$(run_sql "SELECT COUNT(*) FROM trades WHERE trading_mode = 'live';" || echo "0")
HOURS_RUNNING=$(run_sql "SELECT EXTRACT(EPOCH FROM (NOW() - MIN(timestamp))) / 3600 FROM trades WHERE trading_mode = 'live';" || echo "0")
HOURS_INT=$(echo "$HOURS_RUNNING" | cut -d. -f1)

add_line ""
add_line "**Activity:** ${TRADE_COUNT} trades | ${HOURS_INT:-0}h runtime"

if [[ "${HOURS_INT:-0}" -ge 168 ]]; then
  pass "Runtime >= 168h (7 days) — validation complete"
elif [[ "${HOURS_INT:-0}" -ge 72 ]]; then
  pass "Runtime >= 72h (minimum met)"
else
  warn "Runtime: ${HOURS_INT:-0}h (need 72h min)"
fi

TRADE_LIMIT=$(curl -sf http://localhost:8100/api/status 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('live', {}).get('live_trade_limit', -1))
except: print(-1)
" 2>/dev/null || echo "-1")

if [[ "${TRADE_COUNT}" -gt 0 ]]; then
  pass "Has executed trades (${TRADE_COUNT})"
elif [[ "${TRADE_LIMIT}" == "0" ]]; then
  pass "Read-only monitoring mode (trade_limit=0, reconciliation active)"
else
  warn "Zero trades — live path not validated"
fi

# ── Summary ──────────────────────────────────────────────────────────────────
add_line ""
add_line "**PASS: ${PASS_COUNT} | FAIL: ${FAIL_COUNT} | WARN: ${WARN_COUNT}**"

if [[ "$FAIL_COUNT" -eq 0 && "$WARN_COUNT" -eq 0 ]]; then
  add_line "🟢 **ALL GATES PASS — safe to unpause live**"
elif [[ "$FAIL_COUNT" -eq 0 ]]; then
  add_line "🟡 **PASS WITH WARNINGS — review before unpausing**"
else
  add_line "🔴 **FAIL — do NOT unpause live trading**"
fi

# ── Post to Discord ──────────────────────────────────────────────────────────
echo "$REPORT"

if [[ -n "$DISCORD_WEBHOOK" ]]; then
  ESCAPED=$(echo "$REPORT" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))")
  curl -sf -H "Content-Type: application/json" \
    -d "{\"content\": ${ESCAPED}}" \
    "$DISCORD_WEBHOOK" >/dev/null 2>&1 || echo "WARNING: Discord post failed"
else
  echo "WARNING: No DISCORD_ERRORS_WEBHOOK configured — report not posted"
fi
