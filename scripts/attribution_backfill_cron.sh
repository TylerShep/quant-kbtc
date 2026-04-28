#!/usr/bin/env bash
set -uo pipefail

# Nightly daily_attribution backfill — runs on the droplet via cron.
# Computes attribution for any (date, trading_mode) pairs that have trades
# but no attribution row yet. Idempotent: re-running is safe (ON CONFLICT
# upserts). Posts to Discord errors webhook only on failure.
#
# Cron entry (daily at 03:00 UTC, before DB backups at 03:30):
#   0 3 * * * /home/botuser/kbtc/scripts/attribution_backfill_cron.sh \
#     >> /home/botuser/kbtc/logs/attribution_backfill.log 2>&1

PROJECT_DIR="/home/botuser/kbtc"
LOG_DIR="${PROJECT_DIR}/logs"
mkdir -p "${LOG_DIR}"

DISCORD_WEBHOOK=""
if [[ -f "${PROJECT_DIR}/.env" ]]; then
  DISCORD_WEBHOOK=$(grep -E '^DISCORD_ERRORS_WEBHOOK=' "${PROJECT_DIR}/.env" | cut -d= -f2- || true)
fi

post_discord() {
  local content="$1"
  if [[ -z "${DISCORD_WEBHOOK}" ]]; then
    echo "WARNING: no DISCORD_ERRORS_WEBHOOK; skipping Discord post"
    return
  fi
  local payload
  payload=$(echo "${content}" | python3 -c "import sys,json; print(json.dumps({'content': sys.stdin.read()}))")
  curl -sf -H "Content-Type: application/json" -d "${payload}" "${DISCORD_WEBHOOK}" >/dev/null 2>&1 \
    || echo "WARNING: Discord post failed"
}

echo "=== Attribution backfill — $(date -u '+%Y-%m-%d %H:%M:%S UTC') ==="

OUTPUT_FILE=$(mktemp)
trap 'rm -f "${OUTPUT_FILE}"' EXIT

set +e
docker run --rm \
  --user 1000:1000 \
  --network kbtc_kbtc-net \
  -v "${PROJECT_DIR}/backend:/app" \
  -v "${PROJECT_DIR}/scripts:/scripts" \
  -w /app \
  -e HOME=/tmp \
  -e PYTHONPATH=/app \
  -e DATABASE_URL='postgresql://kalshi:kalshi_secret@db:5432/kbtc' \
  --entrypoint python \
  kbtc-bot:latest \
  /scripts/backfill_attribution.py \
  > "${OUTPUT_FILE}" 2>&1
EXIT=$?
set -e

cat "${OUTPUT_FILE}"

if [[ "${EXIT}" -ne 0 ]]; then
  TAIL=$(tail -20 "${OUTPUT_FILE}")
  post_discord "❌ **KBTC nightly attribution backfill FAILED** (exit ${EXIT})
$(date -u '+%Y-%m-%d %H:%M UTC')
\`\`\`
${TAIL}
\`\`\`"
  exit "${EXIT}"
fi

# Success: silent unless something looks off (e.g. zero upserts after multiple
# trading days — usually fine, but worth flagging for our small-data window).
UPSERTED=$(grep -oE 'Upserted [0-9]+ rows' "${OUTPUT_FILE}" | grep -oE '[0-9]+' | head -1 || echo "0")
echo "Backfill complete: ${UPSERTED} rows upserted."
exit 0
