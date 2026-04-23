#!/usr/bin/env bash
set -euo pipefail

# Tear down the canary validation stack.
# Stops containers and optionally removes volumes.
#
# Usage: bash scripts/canary_down.sh [--wipe] [user@host]
#   --wipe  Also remove canary database volumes (full reset)

REMOTE="${KBTC_DEPLOY_HOST:-deploy@your-host}"
WIPE=false

for arg in "$@"; do
  case "$arg" in
    --wipe) WIPE=true ;;
    *@*)    REMOTE="$arg" ;;
  esac
done

PROJECT_DIR="${KBTC_PROJECT_DIR:-/home/botuser/kbtc}"

echo "=== Canary Stack Teardown ==="

if [[ "$WIPE" == "true" ]]; then
  echo "  Mode: full teardown (containers + volumes)"
  ssh "${REMOTE}" "cd ${PROJECT_DIR} && docker compose -f docker-compose.canary.yml down -v"
else
  echo "  Mode: stop containers (data preserved)"
  ssh "${REMOTE}" "cd ${PROJECT_DIR} && docker compose -f docker-compose.canary.yml down"
fi

echo ""
echo "=== Canary stack is DOWN ==="
echo ""
if [[ "$WIPE" == "true" ]]; then
  echo "  Volumes removed. Next canary_up.sh will start fresh."
else
  echo "  Volumes preserved. Next canary_up.sh will resume with existing data."
fi
