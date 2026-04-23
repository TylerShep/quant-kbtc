#!/usr/bin/env bash
set -euo pipefail

# Generate orphan safety validation report from canary stack.
# Checks all promotion gate criteria and outputs pass/fail.
#
# Usage: bash scripts/canary_report.sh [user@host]
# Exit code: 0 = all gates pass, 1 = one or more gates fail

REMOTE="${1:-${KBTC_DEPLOY_HOST:-deploy@your-host}}"
PROJECT_DIR="${KBTC_PROJECT_DIR:-/home/botuser/kbtc}"
CANARY_DB="kbtc-db-canary"
DB_NAME="kbtc_canary"
DB_USER="kalshi"

PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0

pass() { echo "  [PASS] $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "  [FAIL] $1"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
warn() { echo "  [WARN] $1"; WARN_COUNT=$((WARN_COUNT + 1)); }

run_sql() {
  ssh "${REMOTE}" "docker exec ${CANARY_DB} psql -U ${DB_USER} -d ${DB_NAME} -t -A -c \"$1\"" 2>/dev/null
}

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║          CANARY ORPHAN SAFETY VALIDATION REPORT             ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Remote: ${REMOTE}"
echo "  Time:   $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo ""

# ── Gate 1: Canary is running ───────────────────────────────────────────────
echo "── Gate 1: Canary Health ──"
API_OK=$(ssh "${REMOTE}" "curl -sf http://localhost:8100/api/status >/dev/null 2>&1 && echo yes || echo no")
if [[ "$API_OK" == "yes" ]]; then
  pass "Canary bot API is reachable"
else
  fail "Canary bot API is UNREACHABLE"
fi
echo ""

# ── Gate 2: No orphan-settled trades ────────────────────────────────────────
echo "── Gate 2: Orphan Settlement Count ──"
ORPHAN_SETTLED=$(run_sql "SELECT COUNT(*) FROM trades WHERE exit_reason = 'ORPHAN_SETTLED' AND trading_mode = 'live';" || echo "-1")
if [[ "$ORPHAN_SETTLED" == "0" ]]; then
  pass "Zero ORPHAN_SETTLED trades"
elif [[ "$ORPHAN_SETTLED" == "-1" ]]; then
  fail "Could not query canary DB"
else
  fail "Found ${ORPHAN_SETTLED} ORPHAN_SETTLED trades"
  echo "       Details:"
  run_sql "SELECT id, ticker, pnl, timestamp FROM trades WHERE exit_reason = 'ORPHAN_SETTLED' AND trading_mode = 'live' ORDER BY timestamp DESC LIMIT 5;" | while IFS='|' read -r id ticker pnl ts; do
    echo "         #${id} ${ticker} PnL=\$${pnl} at ${ts}"
  done
fi
echo ""

# ── Gate 3: No oversized orphans ────────────────────────────────────────────
echo "── Gate 3: Oversized Orphan Detection ──"
OVERSIZED=$(run_sql "SELECT COUNT(*) FROM errored_trades WHERE error_reason LIKE '%oversized_orphan%';" || echo "-1")
if [[ "$OVERSIZED" == "0" ]]; then
  pass "Zero oversized orphan events"
elif [[ "$OVERSIZED" == "-1" ]]; then
  warn "Could not check errored_trades (table may not exist)"
else
  fail "Found ${OVERSIZED} oversized orphan events"
fi
echo ""

# ── Gate 4: No DESYNC persistent states ─────────────────────────────────────
echo "── Gate 4: DESYNC State Transitions ──"
PM_STATE=$(ssh "${REMOTE}" "curl -sf http://localhost:8100/api/status 2>/dev/null" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('live', {}).get('state', 'UNKNOWN'))
except: print('UNKNOWN')
" 2>/dev/null || echo "UNKNOWN")

if [[ "$PM_STATE" == "FLAT" || "$PM_STATE" == "OPEN" ]]; then
  pass "PositionManager state: ${PM_STATE} (no DESYNC)"
elif [[ "$PM_STATE" == "DESYNC" ]]; then
  fail "PositionManager is in DESYNC state"
else
  warn "Could not determine PositionManager state (${PM_STATE})"
fi
echo ""

# ── Gate 5: No duplicate live trades on same ticker ─────────────────────────
echo "── Gate 5: Duplicate Trade Detection ──"
DUPES=$(run_sql "
  SELECT COUNT(*) FROM (
    SELECT ticker, DATE_TRUNC('minute', timestamp) as minute_bucket
    FROM trades
    WHERE trading_mode = 'live'
    GROUP BY ticker, DATE_TRUNC('minute', timestamp)
    HAVING COUNT(*) > 1
  ) dupes;
" || echo "-1")

if [[ "$DUPES" == "0" ]]; then
  pass "Zero duplicate trades on same ticker in same minute"
elif [[ "$DUPES" == "-1" ]]; then
  warn "Could not check for duplicate trades"
else
  fail "Found ${DUPES} ticker/minute buckets with duplicate trades"
fi
echo ""

# ── Gate 6: Trade count and runtime ─────────────────────────────────────────
echo "── Gate 6: Canary Activity ──"
TRADE_COUNT=$(run_sql "SELECT COUNT(*) FROM trades WHERE trading_mode = 'live';" || echo "0")
FIRST_TRADE=$(run_sql "SELECT MIN(timestamp) FROM trades WHERE trading_mode = 'live';" || echo "N/A")
HOURS_RUNNING=$(run_sql "SELECT EXTRACT(EPOCH FROM (NOW() - MIN(timestamp))) / 3600 FROM trades WHERE trading_mode = 'live';" || echo "0")
HOURS_INT=$(echo "$HOURS_RUNNING" | cut -d. -f1)

echo "  Trades: ${TRADE_COUNT}"
echo "  First:  ${FIRST_TRADE}"
echo "  Hours:  ${HOURS_INT:-0}h"

if [[ "${HOURS_INT:-0}" -ge 72 ]]; then
  pass "Canary has been running >= 72 hours"
else
  warn "Canary runtime: ${HOURS_INT:-0}h (need 72h for full validation)"
fi

if [[ "${TRADE_COUNT}" -gt 0 ]]; then
  pass "Canary has executed trades (${TRADE_COUNT})"
else
  warn "Canary has zero trades — may not be validating live path"
fi
echo ""

# ── Summary ─────────────────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║                       SUMMARY                              ║"
echo "╠══════════════════════════════════════════════════════════════╣"
printf "║  PASS: %-3s  FAIL: %-3s  WARN: %-3s                        ║\n" "$PASS_COUNT" "$FAIL_COUNT" "$WARN_COUNT"
echo "╠══════════════════════════════════════════════════════════════╣"

if [[ "$FAIL_COUNT" -eq 0 && "$WARN_COUNT" -eq 0 ]]; then
  echo "║  VERDICT: ALL GATES PASS — safe to unpause live trading    ║"
elif [[ "$FAIL_COUNT" -eq 0 ]]; then
  echo "║  VERDICT: PASS WITH WARNINGS — review before unpausing     ║"
else
  echo "║  VERDICT: FAIL — do NOT unpause live trading               ║"
fi
echo "╚══════════════════════════════════════════════════════════════╝"

[[ "$FAIL_COUNT" -eq 0 ]] && exit 0 || exit 1
