#!/usr/bin/env bash
set -uo pipefail

# Auto-apply Tier 1 edge_profile recommendations from the most recent
# weekly review. Runs Sundays at 05:30 UTC (30 min after the review job).
#
# Cron entry:
#   30 5 * * 0 /home/botuser/kbtc/scripts/edge_profile_apply_cron.sh \
#     >> /home/botuser/kbtc/logs/edge_apply.log 2>&1
#
# Behavior:
#   - Picks the newest recommendations_<ts>.json under
#     ~/kbtc/data/edge_review/.
#   - Aborts cleanly if EDGE_LIVE_AUTO_APPLY_ENABLED=false in .env
#     (the default state — operator opts in after observation period).
#   - Otherwise, runs scripts/edge_profile_apply.py inside a fresh
#     kbtc-bot:latest container that has DB and docker socket access.
#   - Posts results to DISCORD_RISK_WEBHOOK.

PROJECT_DIR="/home/botuser/kbtc"
REVIEW_DIR="${PROJECT_DIR}/data/edge_review"
LOG_DIR="${PROJECT_DIR}/logs"

mkdir -p "${LOG_DIR}"

DISCORD_RISK=""
DB_URL="postgresql://kalshi:kalshi_secret@db:5432/kbtc"
if [[ -f "${PROJECT_DIR}/.env" ]]; then
  DISCORD_RISK=$(grep -E '^DISCORD_RISK_WEBHOOK=' "${PROJECT_DIR}/.env" \
    | cut -d= -f2- || true)
  DATABASE_URL_OVERRIDE=$(grep -E '^DATABASE_URL=' "${PROJECT_DIR}/.env" \
    | cut -d= -f2- || true)
  if [[ -n "${DATABASE_URL_OVERRIDE}" ]]; then
    DB_URL="${DATABASE_URL_OVERRIDE}"
  fi
fi

post_discord_error() {
  local content="$1"
  if [[ -z "${DISCORD_RISK}" ]]; then return; fi
  local payload
  payload=$(echo "${content}" | python3 -c \
    "import sys,json; print(json.dumps({'content': sys.stdin.read()}))")
  curl -sf -H "Content-Type: application/json" -d "${payload}" \
    "${DISCORD_RISK}" >/dev/null 2>&1 || true
}

echo "=== KBTC edge_profile auto-apply — $(date -u '+%Y-%m-%d %H:%M:%S UTC') ==="

LATEST=$(ls -1t "${REVIEW_DIR}"/recommendations_*.json 2>/dev/null | head -n1 || true)
if [[ -z "${LATEST}" ]]; then
  msg="**KBTC edge_profile_apply skipped**: no recommendations file in ${REVIEW_DIR}"
  echo "${msg}"
  post_discord_error "${msg}"
  exit 0
fi
echo "  Using: ${LATEST}"

set +e
docker run --rm \
  --network kbtc_kbtc-net \
  -v "${PROJECT_DIR}/backend:/app" \
  -v "${PROJECT_DIR}/scripts:/scripts" \
  -v "${REVIEW_DIR}:/recs" \
  -v "${PROJECT_DIR}/.env:/host/.env" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "${PROJECT_DIR}:/host_kbtc" \
  -e HOME=/tmp \
  -e DATABASE_URL="${DB_URL}" \
  -e DISCORD_RISK_WEBHOOK="${DISCORD_RISK}" \
  -w /app \
  --entrypoint python \
  kbtc-bot:latest \
  /scripts/edge_profile_apply.py \
    --recommendations-json "/recs/$(basename "${LATEST}")" \
    --env-file /host/.env \
    --restart-cwd /host_kbtc
EXIT=$?
set -e

if [[ "${EXIT}" -eq 1 ]]; then
  echo "  Master kill switch OFF — exit 1 is the expected 'no-op' code."
  exit 0
fi

if [[ "${EXIT}" -ne 0 ]]; then
  post_discord_error "**KBTC edge_profile_apply FAILED** ($(date -u '+%Y-%m-%d %H:%M UTC'))
exit code: ${EXIT}
recommendations: $(basename "${LATEST}")
See ${LOG_DIR}/edge_apply.log on the droplet."
fi

ls -1t "${PROJECT_DIR}"/.env.backup-auto-* 2>/dev/null \
  | tail -n +20 | xargs -r rm -f

echo "=== Auto-apply complete: exit ${EXIT} ==="
exit "${EXIT}"
