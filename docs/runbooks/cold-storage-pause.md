# Cold-Storage Pause Runbook

Use this when you want to reduce DigitalOcean cost by taking the bot fully offline,
preserving enough state to resume later.

Last updated: 2026-06-30.

## Goal

Move from "bot paused" to "droplet can be destroyed" while preserving:

- PostgreSQL / TimescaleDB trading history
- Paper and live trade records
- `trade_features` ML training data
- Bankroll history and attribution
- ML model artifacts
- Remote `.env` configuration
- Restore instructions and checksums

## Important Billing Note

Stopping Docker containers does not materially reduce DigitalOcean Droplet cost.
Powering off a Droplet also generally does not remove Droplet billing while the
Droplet still exists. Meaningful cost reduction comes from:

1. creating verified backups / snapshots,
2. destroying the Droplet,
3. keeping only cheaper storage artifacts such as Spaces objects, local backups,
   and optionally a Droplet snapshot.

## What Not To Put In Git

Do not commit raw database dumps to the repo.

Reasons:

- dumps are large and make Git history permanently heavy,
- they may contain sensitive operational data,
- deleting them later does not remove them from Git history,
- repo clones become slow and fragile.

Acceptable alternatives:

- keep DB dumps in DigitalOcean Spaces,
- keep a local encrypted copy outside Git,
- keep a local ignored copy under `kbtc-backups/` because `.gitignore` already
  ignores that directory,
- commit only a small restore manifest with filename, timestamp, row counts, and
  SHA256 checksum.

If you absolutely need to keep a backup "near the repo", put it in:

```bash
kbtc-backups/
```

That path is gitignored. Prefer encrypting it first.

## Phase 0: Preflight

From the local machine:

```bash
export KBTC_DEPLOY_HOST=botuser@167.71.247.154
ssh "$KBTC_DEPLOY_HOST" "docker ps"
```

Expected current parked state:

- `kbtc-bot` stopped
- `kbtc-db` running and healthy

Check the dashboard/API only if the bot is running:

```bash
ssh "$KBTC_DEPLOY_HOST" "curl -s http://localhost:8000/api/status | python3 -m json.tool"
```

If the bot is not running, this endpoint may be unavailable; that is expected.

## Phase 1: Produce A Final Database Backup

Run the existing backup script:

```bash
ssh "$KBTC_DEPLOY_HOST" "/home/botuser/kbtc/scripts/backup_db.sh"
```

Verify a new local dump exists on the droplet:

```bash
ssh "$KBTC_DEPLOY_HOST" "ls -lh /home/botuser/kbtc-backups/ | tail"
```

Verify it uploaded to DigitalOcean Spaces:

```bash
ssh "$KBTC_DEPLOY_HOST" "s3cmd ls s3://kbtc-backups/postgres/ | tail -10"
```

Record the latest filename:

```bash
ssh "$KBTC_DEPLOY_HOST" "ls -t /home/botuser/kbtc-backups/*.dump | head -1"
```

## Phase 2: Save A Local Copy

Create a local ignored backup directory:

```bash
mkdir -p kbtc-backups/cold-storage
```

Copy the latest dump from the droplet:

```bash
LATEST_DUMP=$(ssh "$KBTC_DEPLOY_HOST" "ls -t /home/botuser/kbtc-backups/*.dump | head -1")
scp "$KBTC_DEPLOY_HOST:$LATEST_DUMP" kbtc-backups/cold-storage/
```

Optionally encrypt the local copy:

```bash
gpg -c kbtc-backups/cold-storage/kbtc-YYYYMMDD-HHMMSS.dump
```

If encrypted, keep the passphrase in 1Password or another durable secret manager.

## Phase 3: Save Remote Configuration And Secrets Metadata

Do not commit secrets. Save a private local copy outside Git, or in an encrypted
archive.

```bash
mkdir -p kbtc-backups/cold-storage/private
scp "$KBTC_DEPLOY_HOST:/home/botuser/kbtc/.env" kbtc-backups/cold-storage/private/remote.env
scp "$KBTC_DEPLOY_HOST:/home/botuser/kbtc/kalshi_private_key.pem" kbtc-backups/cold-storage/private/kalshi_private_key.pem
```

If `kalshi_private_key.pem` is not present at that exact path, check the value of
`KALSHI_PRIVATE_KEY_PATH` in the remote `.env`.

Encrypt the private folder before storing it long term:

```bash
tar -czf kbtc-backups/cold-storage/private-config.tar.gz -C kbtc-backups/cold-storage private
gpg -c kbtc-backups/cold-storage/private-config.tar.gz
rm -rf kbtc-backups/cold-storage/private kbtc-backups/cold-storage/private-config.tar.gz
```

## Phase 4: Save ML Artifacts

ML model artifacts are ignored by Git. Save a copy with the cold-storage bundle:

```bash
mkdir -p kbtc-backups/cold-storage/ml-models
rsync -avz "$KBTC_DEPLOY_HOST:/home/botuser/kbtc/backend/ml/models/" \
  kbtc-backups/cold-storage/ml-models/
```

## Phase 5: Record Row Counts And Checksums

Capture production row counts:

```bash
ssh "$KBTC_DEPLOY_HOST" "docker exec kbtc-db psql -U kalshi -d kbtc -c \"
SELECT 'trades' AS table_name, count(*) FROM trades
UNION ALL SELECT 'trade_features', count(*) FROM trade_features
UNION ALL SELECT 'bankroll_history', count(*) FROM bankroll_history
UNION ALL SELECT 'daily_attribution', count(*) FROM daily_attribution
UNION ALL SELECT 'signal_log', count(*) FROM signal_log
UNION ALL SELECT 'ob_snapshots', count(*) FROM ob_snapshots
UNION ALL SELECT 'candles', count(*) FROM candles
ORDER BY table_name;
\""
```

Create a checksum for the local dump:

```bash
shasum -a 256 kbtc-backups/cold-storage/kbtc-*.dump
```

Create a small restore manifest that is safe to commit:

```bash
cat > kbtc-backups/cold-storage/RESTORE-MANIFEST.txt <<'EOF'
Cold-storage backup date: YYYY-MM-DD
Remote host: botuser@167.71.247.154
DB dump filename: kbtc-YYYYMMDD-HHMMSS.dump
DB dump SHA256: <paste shasum here>
Spaces location: s3://kbtc-backups/postgres/kbtc-YYYYMMDD-HHMMSS.dump
Bot state at pause: kbtc-bot stopped, kbtc-db running at backup time
Notes:
- Raw dump is intentionally not committed to Git.
- Remote .env and Kalshi private key are stored encrypted outside Git.
EOF
```

If you want the manifest committed, copy a redacted version into
`docs/runbooks/` or another tracked docs path.

## Phase 6: Restore Drill Before Destroying The Droplet

Do not destroy the droplet until a restore has been tested.

Run a local restore test:

```bash
docker run --rm -d --name kbtc-restore-test \
  -e POSTGRES_PASSWORD=kalshi_secret \
  -e POSTGRES_USER=kalshi \
  -e POSTGRES_DB=kbtc \
  -p 5500:5432 \
  timescale/timescaledb:latest-pg16

sleep 10

docker exec kbtc-restore-test psql -U kalshi -d kbtc -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
docker exec kbtc-restore-test psql -U kalshi -d kbtc -c "SELECT timescaledb_pre_restore();"

docker exec -i kbtc-restore-test \
  pg_restore -U kalshi -d kbtc --no-owner --if-exists --clean \
  < kbtc-backups/cold-storage/kbtc-YYYYMMDD-HHMMSS.dump

docker exec kbtc-restore-test psql -U kalshi -d kbtc -c "SELECT timescaledb_post_restore();"

docker exec kbtc-restore-test psql -U kalshi -d kbtc -c "
SELECT 'trades' AS table_name, count(*) FROM trades
UNION ALL SELECT 'trade_features', count(*) FROM trade_features
UNION ALL SELECT 'bankroll_history', count(*) FROM bankroll_history
UNION ALL SELECT 'daily_attribution', count(*) FROM daily_attribution
UNION ALL SELECT 'signal_log', count(*) FROM signal_log
ORDER BY table_name;
"

docker stop kbtc-restore-test
```

Compare restored row counts against the production counts from Phase 5.

## Phase 7: Optional DigitalOcean Snapshot

A DB dump is enough to restore the bot if the repo and secrets are available.
A Droplet snapshot is a convenience image and may speed up restoration, but it
has its own monthly storage cost.

Recommended:

- take one final Droplet snapshot if you want a fast rollback path,
- keep the snapshot only as long as it feels worth the cost,
- rely on the logical DB dump + repo + encrypted secrets for durable recovery.

In the DigitalOcean UI:

1. open the Droplet,
2. choose Snapshots,
3. create a snapshot named like `kbtc-cold-storage-YYYY-MM-DD`,
4. wait for completion before destroying the Droplet.

## Phase 8: Destroy The Droplet

Only do this after:

- final backup exists in Spaces,
- local backup exists,
- checksum recorded,
- restore drill passed,
- remote `.env` and Kalshi private key are saved securely,
- ML model artifacts are copied,
- optional snapshot is complete.

Then in DigitalOcean:

1. open the Droplet,
2. confirm the IP is `167.71.247.154`,
3. choose Destroy,
4. choose whether to preserve snapshots/backups,
5. destroy the Droplet.

After destruction, check the billing page to confirm Droplet compute charges stop.
Spaces, snapshots, volumes, and reserved IPs may still bill.

## Phase 9: Resume Later

To resume from cold storage:

1. create a new Ubuntu Droplet,
2. install Docker, Docker Compose plugin, Git, and `s3cmd`,
3. clone the repo into `/home/botuser/kbtc`,
4. restore `.env` and Kalshi private key from encrypted backup,
5. start DB only: `docker compose up -d db`,
6. restore the DB dump with `pg_restore`,
7. start the bot in paper mode first,
8. verify `/api/status`,
9. let at least one 15-minute contract cycle pass before considering live mode.

See `docs/runbooks/database-backups.md` and `HANDOFF.md` for the full restart
context.
