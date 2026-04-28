#!/usr/bin/env bash
set -uo pipefail

# Weekly XGBoost entry-gate retraining — runs on the droplet via cron.
# Exports labeled trade_features, retrains, and conditionally promotes the
# candidate model. Posts the outcome to the Discord errors webhook.
#
# Cron entry (Sunday at 04:00 UTC):
#   0 4 * * 0 /home/botuser/kbtc/scripts/retrain_xgb_cron.sh \
#     >> /home/botuser/kbtc/logs/retrain_xgb.log 2>&1
#
# Behavior:
#   - Trains a candidate. Promotes ONLY if the candidate clears the gate
#     (see scripts/retrain_promote.py for criteria).
#   - Does NOT restart the bot. A human must restart to load the new model.
#   - Failures and held candidates both post a Discord alert.

PROJECT_DIR="/home/botuser/kbtc"
BOT_CONTAINER="kbtc-bot"
DB_CONTAINER="kbtc-db"
DB_USER="kalshi"
DB_NAME="kbtc"
EXPORT_DIR="${PROJECT_DIR}/data/retrain"
EXPORT_CSV="${EXPORT_DIR}/trade_features_$(date -u +%Y%m%dT%H%M%SZ).csv"
LOG_DIR="${PROJECT_DIR}/logs"

DRY_RUN_FLAG=""
for arg in "$@"; do
  case "${arg}" in
    --dry-run)
      DRY_RUN_FLAG="--dry-run"
      echo "DRY RUN MODE — candidate will be trained but not promoted."
      ;;
    *)
      echo "WARNING: unknown arg '${arg}' ignored"
      ;;
  esac
done

mkdir -p "${EXPORT_DIR}" "${LOG_DIR}"

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

fail() {
  local msg="$1"
  echo "FAIL: ${msg}"
  post_discord "**KBTC retrain FAILED** ($(date -u '+%Y-%m-%d %H:%M UTC'))\n${msg}"
  exit 1
}

echo "=== KBTC weekly retrain — $(date -u '+%Y-%m-%d %H:%M:%S UTC') ==="

# ── 1. Export labeled trade_features ─────────────────────────────────────────
echo "Exporting labeled trade_features to ${EXPORT_CSV} ..."
docker exec "${DB_CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" -c \
  "COPY (SELECT * FROM trade_features WHERE label IS NOT NULL) TO STDOUT CSV HEADER" \
  > "${EXPORT_CSV}" 2>/dev/null \
  || fail "Could not export trade_features from ${DB_CONTAINER}"

ROWS=$(($(wc -l < "${EXPORT_CSV}") - 1))
if [[ "${ROWS}" -lt 200 ]]; then
  fail "Only ${ROWS} labeled rows; need >= 200. Skipping retrain."
fi
echo "Exported ${ROWS} labeled rows."

# ── 2. Run the promotion-gated retrain in a fresh container ────────────────
# We use the bot image (not the running container) so we get the exact same
# xgboost/sklearn/joblib versions the bot uses at inference time, without
# interfering with the live bot process. No network needed — input is the
# CSV we just exported, output is files under backend/ml/models/ via mount.
echo "Running retrain_promote.py in a fresh ${BOT_CONTAINER} image instance ..."

OUTPUT_FILE=$(mktemp)
trap 'rm -f "${OUTPUT_FILE}"' EXIT

# Run the transient container as the HOST botuser (uid 1000) — not the
# image's built-in 'botuser' (uid 999) — so the .pkl files written under
# the bind-mounted backend/ml/models/ keep correct host ownership and the
# directory is writable. Without this we hit "Permission denied" on output.
HOST_UID=$(id -u)
HOST_GID=$(id -g)

set +e
docker run --rm \
  --user "${HOST_UID}:${HOST_GID}" \
  -v "${PROJECT_DIR}/backend:/app" \
  -v "${PROJECT_DIR}/scripts:/scripts" \
  -v "${EXPORT_DIR}:/data:ro" \
  -w /app \
  -e HOME=/tmp \
  --entrypoint python \
  kbtc-bot:latest \
  /scripts/retrain_promote.py --csv "/data/$(basename "${EXPORT_CSV}")" ${DRY_RUN_FLAG} \
  > "${OUTPUT_FILE}" 2>&1
RETRAIN_EXIT=$?
set -e

OUTPUT=$(cat "${OUTPUT_FILE}")
echo "${OUTPUT}"

# ── 3. Interpret exit code and post to Discord ──────────────────────────────
DECISION_LINE=$(echo "${OUTPUT}" | grep -E '^(PROMOTED|HELD|DRY RUN)' | head -1 || true)
PROMOTION_BLOCK=$(echo "${OUTPUT}" | sed -n '/PROMOTION DECISION/,/^$/p' | head -20 || true)

case "${RETRAIN_EXIT}" in
  0)
    if echo "${DECISION_LINE}" | grep -q "^PROMOTED"; then
      EMOJI="✅"
      HEADER="**KBTC retrain PROMOTED new model**"
      ACTION_NOTE=$'\n**Action required:** restart kbtc-bot to load the new model.'
    else
      EMOJI="ℹ️"
      HEADER="**KBTC retrain completed (dry run / no promotion)**"
      ACTION_NOTE=""
    fi
    ;;
  2)
    EMOJI="🟡"
    HEADER="**KBTC retrain HELD candidate (gate failed)**"
    ACTION_NOTE=""
    ;;
  *)
    EMOJI="❌"
    HEADER="**KBTC retrain ERRORED** (exit ${RETRAIN_EXIT})"
    ACTION_NOTE=""
    ;;
esac

MSG="${EMOJI} ${HEADER}
$(date -u '+%Y-%m-%d %H:%M UTC') — ${ROWS} labeled rows
\`\`\`
${PROMOTION_BLOCK:-${OUTPUT: -1500}}
\`\`\`${ACTION_NOTE}"

post_discord "${MSG}"

# ── 4. Prune old CSV exports (keep last 10) ─────────────────────────────────
ls -1t "${EXPORT_DIR}"/trade_features_*.csv 2>/dev/null | tail -n +11 | xargs -r rm -f

echo "=== Retrain complete: exit ${RETRAIN_EXIT} ==="
exit "${RETRAIN_EXIT}"
