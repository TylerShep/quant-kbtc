#!/usr/bin/env bash
# Daily Postgres backup for the KBTC trading bot.
#
# Runs on the droplet via cron, dumps the running TimescaleDB container with
# pg_dump (custom format, internal compression), uploads to a DigitalOcean
# Spaces bucket, prunes old local copies, and posts the result to the
# #kbtc-errors Discord channel.
#
# See docs/runbooks/database-backups.md for setup, restore drill, and disaster
# recovery procedure.
#
# Cron entry (daily at 03:30 UTC):
#   30 3 * * * /home/botuser/kbtc/scripts/backup_db.sh >> /home/botuser/kbtc/logs/backup_db.log 2>&1

set -euo pipefail

PROJECT_DIR="/home/botuser/kbtc"
BACKUP_DIR="/home/botuser/kbtc-backups"
DB_CONTAINER="kbtc-db"
DB_NAME="kbtc"
DB_USER="kalshi"
SPACES_BUCKET="s3://kbtc-backups/postgres"
LOCAL_RETENTION_DAYS=7
MIN_DUMP_BYTES=10485760  # 10 MiB sanity check

mkdir -p "${BACKUP_DIR}"
TS=$(date -u '+%Y%m%d-%H%M%S')
DUMP_FILE="${BACKUP_DIR}/kbtc-${TS}.dump"

DISCORD_WEBHOOK=""
if [[ -f "${PROJECT_DIR}/.env" ]]; then
  DISCORD_WEBHOOK=$(grep -E '^DISCORD_ERRORS_WEBHOOK=' "${PROJECT_DIR}/.env" | cut -d= -f2-)
fi

post_discord() {
  local msg="$1"
  if [[ -n "$DISCORD_WEBHOOK" ]]; then
    local escaped
    escaped=$(echo "$msg" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))")
    curl -sf -H "Content-Type: application/json" \
      -d "{\"content\": ${escaped}}" "$DISCORD_WEBHOOK" >/dev/null 2>&1 || true
  fi
}

on_error() {
  local exit_code=$?
  post_discord "**[BACKUP] FAILED** \`kbtc-${TS}.dump\` exit=${exit_code} (see ${PROJECT_DIR}/logs/backup_db.log)"
  exit "$exit_code"
}
trap on_error ERR

start_ts=$(date +%s)

docker exec "${DB_CONTAINER}" pg_dump -U "${DB_USER}" -d "${DB_NAME}" -Fc -Z 6 > "${DUMP_FILE}"

dump_bytes=$(stat -c%s "${DUMP_FILE}" 2>/dev/null || stat -f%z "${DUMP_FILE}")
if [[ "${dump_bytes}" -lt "${MIN_DUMP_BYTES}" ]]; then
  echo "ERROR: dump file suspiciously small (${dump_bytes} bytes, min=${MIN_DUMP_BYTES})" >&2
  exit 2
fi

dump_human=$(du -h "${DUMP_FILE}" | cut -f1)

s3cmd put "${DUMP_FILE}" "${SPACES_BUCKET}/" --quiet

find "${BACKUP_DIR}" -name 'kbtc-*.dump' -type f -mtime "+${LOCAL_RETENTION_DAYS}" -delete

elapsed=$(( $(date +%s) - start_ts ))
local_count=$(find "${BACKUP_DIR}" -name 'kbtc-*.dump' -type f | wc -l | tr -d ' ')

post_discord "**[BACKUP] OK** \`kbtc-${TS}.dump\` size=${dump_human} elapsed=${elapsed}s | local kept=${local_count} | remote=Spaces (30d lifecycle)"
echo "Backup complete: ${DUMP_FILE} (${dump_human}, ${elapsed}s)"
