#!/usr/bin/env bash
set -uo pipefail

# Weekly edge_profile + ML co-calibration review — runs on the droplet
# via cron. Sundays at 05:00 UTC, 1h after the ML retrain so the review
# attributes against the freshly-promoted (or held) model.
#
# Cron entry:
#   0 5 * * 0 /home/botuser/kbtc/scripts/edge_profile_review_cron.sh \
#     >> /home/botuser/kbtc/logs/edge_review.log 2>&1
#
# Behavior:
#   - Runs scripts/edge_profile_review.py inside a fresh kbtc-bot:latest
#     container (same image the bot uses, so xgboost/sqlalchemy versions
#     match). DB read-only.
#   - Posts the report to DISCORD_ATTRIBUTION_WEBHOOK.
#   - Writes JSON sidecar to ~/kbtc/data/edge_review/recommendations_<ts>.json
#     which scripts/edge_profile_apply.py consumes 30 min later.

PROJECT_DIR="/home/botuser/kbtc"
BOT_CONTAINER="kbtc-bot"
OUTPUT_DIR="${PROJECT_DIR}/data/edge_review"
LOG_DIR="${PROJECT_DIR}/logs"

mkdir -p "${OUTPUT_DIR}" "${LOG_DIR}"

DISCORD_WEBHOOK=""
ATTR_WEBHOOK=""
DB_URL="postgresql://kalshi:kalshi_secret@db:5432/kbtc"
if [[ -f "${PROJECT_DIR}/.env" ]]; then
  DISCORD_WEBHOOK=$(grep -E '^DISCORD_ERRORS_WEBHOOK=' "${PROJECT_DIR}/.env" \
    | cut -d= -f2- || true)
  ATTR_WEBHOOK=$(grep -E '^DISCORD_ATTRIBUTION_WEBHOOK=' "${PROJECT_DIR}/.env" \
    | cut -d= -f2- || true)
  DATABASE_URL_OVERRIDE=$(grep -E '^DATABASE_URL=' "${PROJECT_DIR}/.env" \
    | cut -d= -f2- || true)
  if [[ -n "${DATABASE_URL_OVERRIDE}" ]]; then
    DB_URL="${DATABASE_URL_OVERRIDE}"
  fi
fi

post_discord_error() {
  local content="$1"
  if [[ -z "${DISCORD_WEBHOOK}" ]]; then
    return
  fi
  local payload
  payload=$(echo "${content}" | python3 -c \
    "import sys,json; print(json.dumps({'content': sys.stdin.read()}))")
  curl -sf -H "Content-Type: application/json" -d "${payload}" \
    "${DISCORD_WEBHOOK}" >/dev/null 2>&1 || true
}

echo "=== KBTC weekly edge_profile review — $(date -u '+%Y-%m-%d %H:%M:%S UTC') ==="

HOST_UID=$(id -u)
HOST_GID=$(id -g)

# Use the bot's network so 'db' resolves; --user keeps file permissions
# correct on the bind-mounted output dir.
set +e
docker run --rm \
  --user "${HOST_UID}:${HOST_GID}" \
  --network kbtc_kbtc-net \
  -v "${PROJECT_DIR}/backend:/app" \
  -v "${PROJECT_DIR}/scripts:/scripts" \
  -v "${OUTPUT_DIR}:/data" \
  -e HOME=/tmp \
  -e DATABASE_URL="${DB_URL}" \
  -e DISCORD_ATTRIBUTION_WEBHOOK="${ATTR_WEBHOOK}" \
  -w /app \
  --entrypoint python \
  kbtc-bot:latest \
  /scripts/edge_profile_review.py \
    --window-days 14 \
    --mode paper \
    --env-file /scripts/../.env \
    --output-dir /data \
    --post-discord
EXIT=$?
set -e

if [[ "${EXIT}" -ne 0 ]]; then
  post_discord_error "**KBTC edge_profile review FAILED** ($(date -u '+%Y-%m-%d %H:%M UTC'))
exit code: ${EXIT}
See ${LOG_DIR}/edge_review.log on the droplet."
fi

ls -1t "${OUTPUT_DIR}"/recommendations_*.json 2>/dev/null \
  | tail -n +21 | xargs -r rm -f
ls -1t "${OUTPUT_DIR}"/report_*.md 2>/dev/null \
  | tail -n +21 | xargs -r rm -f

echo "=== Review complete: exit ${EXIT} ==="
exit "${EXIT}"
