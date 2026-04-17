#!/usr/bin/env bash
set -euo pipefail

# Check health and status of the canary validation stack.
#
# Usage: bash scripts/canary_status.sh [user@host]

REMOTE="${1:-botuser@167.71.247.154}"
PROJECT_DIR="/home/botuser/kbtc"

echo "=== Canary Stack Status ==="
echo ""

echo "── Container status ──"
ssh "${REMOTE}" "cd ${PROJECT_DIR} && docker compose -f docker-compose.canary.yml ps" 2>/dev/null || echo "  Canary stack not running"
echo ""

echo "── Bot API health ──"
API_RESPONSE=$(ssh "${REMOTE}" "curl -sf http://localhost:8100/api/status 2>/dev/null" || echo "UNREACHABLE")
if [[ "$API_RESPONSE" == "UNREACHABLE" ]]; then
  echo "  Bot API: UNREACHABLE"
else
  echo "  Bot API: HEALTHY"
  echo "  $API_RESPONSE" | python3 -m json.tool 2>/dev/null || echo "  $API_RESPONSE"
fi
echo ""

echo "── Database connectivity ──"
DB_CHECK=$(ssh "${REMOTE}" "docker exec kbtc-db-canary psql -U kalshi -d kbtc_canary -c 'SELECT COUNT(*) as trade_count FROM trades;' -t 2>/dev/null" || echo "UNREACHABLE")
if [[ "$DB_CHECK" == "UNREACHABLE" ]]; then
  echo "  Canary DB: UNREACHABLE"
else
  echo "  Canary DB: CONNECTED"
  echo "  Total trades: $(echo "$DB_CHECK" | tr -d ' ')"
fi
echo ""

echo "── Bot logs (last 20 lines) ──"
ssh "${REMOTE}" "docker logs kbtc-bot-canary --tail 20 2>&1" || echo "  No logs available"
