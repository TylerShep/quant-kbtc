#!/usr/bin/env bash
set -euo pipefail

# Deploy KBTC bot to DigitalOcean droplet
# Usage: ./scripts/deploy.sh [user@host]

REMOTE="${1:-botuser@64.23.133.157}"
PROJECT_DIR="/home/botuser/kbtc"

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

echo "=== Building and restarting on remote ==="
ssh "${REMOTE}" "cd ${PROJECT_DIR} && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build"

echo "=== Deploy complete ==="
ssh "${REMOTE}" "cd ${PROJECT_DIR} && docker compose ps"
