# Database backups runbook

Daily `pg_dump` of the production TimescaleDB instance, pushed to a DigitalOcean
Spaces bucket, with a documented restore procedure. Owner: whoever is on call.

## Targets

| | |
|---|---|
| RPO | 24h (daily backup at 03:30 UTC) |
| RTO | ~10–15 min (download + `pg_restore`) |
| Local retention | 7 days on the droplet (`/home/botuser/kbtc-backups/`) |
| Remote retention | 30 days in Spaces (lifecycle policy auto-deletes) |
| Cost | ~$5/mo (DO Spaces flat rate, 250GB + 1TB transfer) |
| Format | `pg_dump -Fc -Z 6` — custom format, internal compression level 6 |

The bot's source-of-truth for live positions is Kalshi itself, so a 24h-RPO
logical dump is acceptable. We're protecting against droplet loss, accidental
`DROP TABLE`, and the auto-tuner / strategy state we'd otherwise lose forever.

## Architecture

```
cron @ 03:30 UTC
  └─ scripts/backup_db.sh
       ├─ docker exec kbtc-db pg_dump -Fc -Z 6  →  /home/botuser/kbtc-backups/kbtc-<ts>.dump
       ├─ size sanity check (>= 10 MiB)
       ├─ s3cmd put → s3://kbtc-backups/postgres/  (Spaces lifecycle deletes >30d)
       ├─ find -mtime +7 -delete (local prune)
       └─ post result to #kbtc-errors Discord webhook
```

## One-time setup

### 1. Create the Spaces bucket (DO control panel)

1. **Spaces → Create**: name `kbtc-backups`, region `nyc3` (cheapest, same DC as
   the droplet so transfers don't egress).
2. **File listing**: leave at default (restricted; public listing is off).
3. **Settings → Lifecycle policies → Add rule**:
   - Prefix: `postgres/`
   - Action: **Permanently delete current versions of objects**
   - After: **30 days**
4. **API → Spaces Keys → Generate New Key** named `kbtc-droplet-backup`. Save
   the access key and secret somewhere durable (1Password). The secret is shown
   exactly once.

### 2. Bootstrap the droplet (one time)

```bash
ssh "$KBTC_DEPLOY_HOST"

sudo apt-get update && sudo apt-get install -y s3cmd

s3cmd --configure
#   Access Key:           <from step 1.4>
#   Secret Key:           <from step 1.4>
#   Default Region:       us-east-1
#   S3 Endpoint:          nyc3.digitaloceanspaces.com
#   DNS-style template:   %(bucket)s.nyc3.digitaloceanspaces.com
#   Use HTTPS:            yes
#   (skip GPG / proxy)

mkdir -p /home/botuser/kbtc-backups
mkdir -p /home/botuser/kbtc/logs
```

The script ships executable in the repo (`chmod +x scripts/backup_db.sh`
preserved by `rsync -a` in `deploy.sh`), so no further `chmod` is needed.

### 3. Manual smoke test (before adding to cron)

```bash
/home/botuser/kbtc/scripts/backup_db.sh

# Verify locally:
ls -la /home/botuser/kbtc-backups/
# Expected: kbtc-YYYYMMDD-HHMMSS.dump, ~tens of MB

# Verify remote:
s3cmd ls s3://kbtc-backups/postgres/

# Verify Discord:
# #kbtc-errors should show one "[BACKUP] OK ..." line.
```

If the smoke test posts FAILED, check `/home/botuser/kbtc/logs/backup_db.log`.

### 4. Install cron

```bash
crontab -e
```

Append (do not remove the existing `canary_report_cron.sh` line):

```cron
# Daily DB backup at 03:30 UTC (low-volume window, before US session open)
30 3 * * * /home/botuser/kbtc/scripts/backup_db.sh >> /home/botuser/kbtc/logs/backup_db.log 2>&1
```

## Restore drill (run quarterly + after any backup-pipeline change)

The first drill should run **immediately after the first successful production
backup** so we know the procedure actually works before we need it.

```bash
# 1. Pull the latest dump locally (or to a fresh test droplet)
s3cmd get s3://kbtc-backups/postgres/kbtc-YYYYMMDD-HHMMSS.dump ./test.dump

# 2. Spin up a throwaway TimescaleDB container
docker run --rm -d --name kbtc-restore-test \
  -e POSTGRES_PASSWORD=test \
  -p 5500:5432 \
  timescale/timescaledb:latest-pg16

sleep 5  # wait for PG to come up

# 3. TimescaleDB requires the extension to exist in the target DB BEFORE restore
docker exec kbtc-restore-test psql -U postgres -c "CREATE DATABASE kbtc;"
docker exec kbtc-restore-test psql -U postgres -d kbtc -c "CREATE EXTENSION timescaledb;"

# 4. Restore (the dump is custom-format, so use pg_restore, not psql)
cat test.dump | docker exec -i kbtc-restore-test \
  pg_restore -U postgres -d kbtc --no-owner --if-exists --clean

# 5. Sanity-check row counts against production
docker exec kbtc-restore-test psql -U postgres -d kbtc -c "
  SELECT 'trades' AS t, count(*) FROM trades
  UNION ALL SELECT 'errored_trades', count(*) FROM errored_trades
  UNION ALL SELECT 'bankroll_history', count(*) FROM bankroll_history
  UNION ALL SELECT 'signal_log', count(*) FROM signal_log;
  SELECT max(timestamp) AS last_ob FROM ob_snapshots;
"

# 6. Tear down
docker stop kbtc-restore-test
rm test.dump
```

Compare row counts against production:

```bash
ssh "$KBTC_DEPLOY_HOST" "docker exec kbtc-db psql -U kalshi -d kbtc -c \"
  SELECT 'trades' AS t, count(*) FROM trades
  UNION ALL SELECT 'errored_trades', count(*) FROM errored_trades;
\""
```

Numbers should match (trades/signals may be a few rows ahead in production
since the dump is from earlier).

## Disaster recovery: full droplet loss

1. **Provision a new droplet**: Ubuntu 22.04, same size as the prior one,
   nyc3 region. Add SSH key for `botuser`.
2. **Install dependencies**:

   ```bash
   sudo apt-get update
   sudo apt-get install -y docker.io docker-compose-plugin s3cmd git
   sudo usermod -aG docker botuser
   ```

3. **Clone the repo and configure secrets**:

   ```bash
   sudo -iu botuser
   git clone <repo> /home/botuser/kbtc
   cd /home/botuser/kbtc
   cp .env.example .env
   # Fill in: KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY, DISCORD_*_WEBHOOK,
   # DATABASE_URL, DASHBOARD_API_TOKEN, etc. from 1Password.
   ```

4. **Configure s3cmd** as in step 2 above and pull the latest dump:

   ```bash
   s3cmd --configure
   s3cmd ls s3://kbtc-backups/postgres/ | tail -5
   s3cmd get s3://kbtc-backups/postgres/kbtc-LATEST.dump ./latest.dump
   ```

5. **Bring up Docker stack with empty DB volume**:

   ```bash
   cd /home/botuser/kbtc
   docker compose up -d db
   sleep 10
   ```

6. **Restore into the live DB**:

   ```bash
   docker exec kbtc-db psql -U kalshi -d kbtc -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
   cat latest.dump | docker exec -i kbtc-db pg_restore -U kalshi -d kbtc --no-owner --if-exists --clean
   ```

7. **Bring up the rest of the stack**:

   ```bash
   docker compose up -d
   curl -s http://localhost:8000/api/status | jq
   ```

8. **Restore cron**:

   ```bash
   crontab -l  # if you saved it, restore from backup; otherwise re-install
   # Append the canary line and the backup line as documented.
   ```

9. **Update DNS / firewall** if the droplet IP changed (update the bookmarked
   dashboard URL `<DEPLOY_HOST>:8000` and any operator references).
10. **Verify trading mode is `paper` first**, watch a few signal cycles, then
    flip to `live` only after the bot has run cleanly for at least one
    15-minute contract resolution.

## Monitoring

Today: passive. Every successful run posts a one-liner to `#kbtc-errors`; every
failure trips the `ERR` trap and posts a `FAILED` line. **Absence of a daily OK
line for >36h means the pipeline broke** — check `logs/backup_db.log` and
`s3cmd ls s3://kbtc-backups/postgres/`.

Future: a `/api/backup-health` endpoint that reads the latest object's
`Last-Modified` from Spaces and surfaces a red badge in the dashboard if it's
stale. Out of scope for this change.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `s3cmd: command not found` | Bootstrap step skipped | `sudo apt-get install -y s3cmd && s3cmd --configure` |
| `403 Forbidden` from s3cmd | Spaces key revoked or wrong region | Regenerate key in DO panel, re-run `s3cmd --configure` |
| `dump file suspiciously small` | DB not fully up; container restarted mid-dump | Check `docker ps`; rerun manually |
| Discord OK lines stop showing | Webhook rotated | Update `DISCORD_ERRORS_WEBHOOK` in `.env`; rerun manually |
| `pg_restore: error: relation already exists` | Restored into a non-empty DB without `--clean` | Use `--clean --if-exists` (already in the documented commands) |
| `extension "timescaledb" must be preloaded` | Forgot to `CREATE EXTENSION` in target DB before restore | Run the `CREATE EXTENSION timescaledb;` step first |

## What this does NOT cover

- **Point-in-time recovery (PITR)**. We do not archive WAL. Worst-case window
  is the last 24h of trades — recoverable from Kalshi's trade history if needed.
- **Encryption at rest beyond Spaces' default**. Spaces encrypts at rest; the
  upload goes over HTTPS. If we ever need an extra layer, `gpg --encrypt`
  before upload (Future).
- **Multi-region replication**. Single bucket, single region (nyc3).
- **Automated restore drill**. Manual quarterly. A `scripts/restore_drill.sh`
  that runs a sidecar container and asserts row counts is a future enhancement.
