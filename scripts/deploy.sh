#!/usr/bin/env bash
set -euo pipefail

# Deploy KBTC bot to DigitalOcean droplet
# Usage: ./scripts/deploy.sh [user@host]
#        ./scripts/deploy.sh --force          # skip safety check

PROJECT_DIR="/home/botuser/kbtc"
FORCE=false
REMOTE="botuser@167.71.247.154"

for arg in "$@"; do
  case "$arg" in
    --force) FORCE=true ;;
    *@*)     REMOTE="$arg" ;;
  esac
done

# ── Ensure swap exists on remote (idempotent, needs root) ────────────────
REMOTE_HOST="${REMOTE#*@}"
echo "=== Checking swap on remote ==="
ssh "root@${REMOTE_HOST}" "if [ ! -f /swapfile ]; then
    echo '  Creating 2G swap...'
    fallocate -l 2G /swapfile &&
    chmod 600 /swapfile &&
    mkswap /swapfile &&
    swapon /swapfile &&
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    echo '  Swap created and enabled.'
else
    swapon /swapfile 2>/dev/null || true
    echo '  Swap already configured.'
fi"

# ── Pre-deploy safety check ──────────────────────────────────────────────
echo "=== Pre-deploy safety check ==="

DEPLOY_CHECK=$(ssh "${REMOTE}" "curl -sf http://localhost:8000/api/deploy-check 2>/dev/null" || echo "UNREACHABLE")

if [[ "$DEPLOY_CHECK" == "UNREACHABLE" ]]; then
  echo "  Bot is not running or unreachable — safe to deploy (cold start)."
else
  SAFE=$(echo "$DEPLOY_CHECK" | python3 -c "import sys,json; print(json.load(sys.stdin).get('safe_to_deploy', False))" 2>/dev/null || echo "False")
  MESSAGE=$(echo "$DEPLOY_CHECK" | python3 -c "import sys,json; print(json.load(sys.stdin).get('message', 'unknown'))" 2>/dev/null || echo "unknown")

  if [[ "$SAFE" == "True" ]]; then
    echo "  $MESSAGE"
  else
    echo ""
    echo "  DEPLOY BLOCKED: $MESSAGE"
    echo ""
    echo "  The bot has open live positions or resting orders on Kalshi."
    echo "  Restarting now would orphan these positions."
    echo ""
    echo "  Options:"
    echo "    1. Wait for positions to settle, then re-run deploy"
    echo "    2. Pause live trading in the dashboard and wait for settling"
    echo "    3. Run with --force to skip this check (NOT recommended)"
    echo ""

    if [[ "$FORCE" == "true" ]]; then
      echo "  --force flag set, proceeding anyway..."
    else
      exit 1
    fi
  fi
fi

# ── Sync files ───────────────────────────────────────────────────────────
echo "=== Deploying KBTC to ${REMOTE} ==="

rsync -avz --progress \
    --exclude='.git' \
    --exclude='.env' \
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

echo "=== Fixing ports for production ==="
ssh "${REMOTE}" "cd ${PROJECT_DIR} && sed -i 's/\"5433:5432\"/\"5432:5432\"/' docker-compose.yml && sed -i 's/\"8001:8000\"/\"8000:8000\"/' docker-compose.yml"

echo "=== Building and restarting on remote ==="
ssh "${REMOTE}" "cd ${PROJECT_DIR} && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build"

echo "=== Deploy complete ==="
ssh "${REMOTE}" "cd ${PROJECT_DIR} && docker compose ps"
