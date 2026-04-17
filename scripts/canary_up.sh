#!/usr/bin/env bash
set -euo pipefail

# Launch the canary validation stack on the remote droplet.
# Uses Kalshi demo API + live code path to validate orphan handling.
#
# Usage: bash scripts/canary_up.sh [user@host]

REMOTE="${1:-botuser@167.71.247.154}"
PROJECT_DIR="/home/botuser/kbtc"

echo "=== Canary Stack Launch ==="
echo "  Remote: ${REMOTE}"
echo ""

# ── Safety: verify .env.canary exists on remote ─────────────────────────────
echo "=== Checking canary environment ==="
HAS_ENV=$(ssh "${REMOTE}" "test -f ${PROJECT_DIR}/.env.canary && echo yes || echo no")
if [[ "$HAS_ENV" == "no" ]]; then
  echo "  ERROR: ${PROJECT_DIR}/.env.canary not found on remote."
  echo "  Copy .env.canary.example to .env.canary and fill in demo API keys."
  exit 1
fi

# ── Safety: verify KALSHI_ENV ────────────────────────────────────────────────
KALSHI_ENV=$(ssh "${REMOTE}" "grep -E '^KALSHI_ENV=' ${PROJECT_DIR}/.env.canary | cut -d= -f2 | tr -d ' '")
if [[ "$KALSHI_ENV" == "prod" ]]; then
  echo "  KALSHI_ENV=prod (read-only monitoring mode)"
  echo "  Canary will run reconciliation against prod API but NOT place orders."
elif [[ "$KALSHI_ENV" == "demo" ]]; then
  echo "  KALSHI_ENV=demo (full demo trading mode)"
else
  echo ""
  echo "  FATAL: KALSHI_ENV=${KALSHI_ENV} — must be 'demo' or 'prod'"
  echo ""
  exit 1
fi

# ── Safety: verify TRADING_MODE=live ────────────────────────────────────────
TRADING_MODE=$(ssh "${REMOTE}" "grep -E '^TRADING_MODE=' ${PROJECT_DIR}/.env.canary | cut -d= -f2 | tr -d ' '")
if [[ "$TRADING_MODE" != "live" ]]; then
  echo "  WARNING: TRADING_MODE=${TRADING_MODE} (expected 'live' for canary)"
  echo "  Canary validates live code path — set TRADING_MODE=live"
  read -rp "  Continue anyway? [y/N] " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || exit 1
fi

# ── Sync files to remote ────────────────────────────────────────────────────
echo "=== Syncing project files ==="
rsync -avz --progress \
    --exclude='.git' \
    --exclude='.env' \
    --exclude='.env.canary' \
    --exclude='*.pem' \
    --exclude='*.key' \
    --exclude='__pycache__' \
    --exclude='node_modules' \
    --exclude='frontend/dist' \
    --exclude='.venv' \
    --exclude='venv' \
    --exclude='backtest_reports' \
    --exclude='.cursor' \
    . "${REMOTE}:${PROJECT_DIR}/"

# ── Fix canary ports ────────────────────────────────────────────────────────
echo "=== Starting canary containers ==="
ssh "${REMOTE}" "cd ${PROJECT_DIR} && docker compose -f docker-compose.canary.yml up -d --build"

echo ""
echo "=== Canary stack is UP ==="
ssh "${REMOTE}" "cd ${PROJECT_DIR} && docker compose -f docker-compose.canary.yml ps"
echo ""
echo "  Dashboard: http://167.71.247.154:8100"
echo "  API:       http://167.71.247.154:8100/api/status"
echo "  DB:        port 5434 (kbtc_canary)"
echo ""
echo "  Run 'bash scripts/canary_status.sh' to check health"
echo "  Run 'bash scripts/canary_report.sh' for orphan validation report"
echo "  Run 'bash scripts/canary_down.sh' to tear down"
