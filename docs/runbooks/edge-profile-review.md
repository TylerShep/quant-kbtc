# Edge profile maintenance runbook

Operator-facing SOP for the edge_profile maintenance system: weekly
co-calibration review, Tier 1 auto-apply, and the tripwire alarms that
catch silent live-lane failures between reviews.

| | |
|---|---|
| Cron schedule | `0 5 * * 0` (review) and `30 5 * * 0` (auto-apply), Sundays UTC |
| Triggers | `scripts/edge_profile_review_cron.sh`, `scripts/edge_profile_apply_cron.sh` |
| Notification (review) | Discord attribution webhook |
| Notification (auto-apply) | Discord risk webhook |
| Notification (tripwires) | Discord risk webhook (hourly checks from coordinator) |
| Bot restart | Auto-apply does restart bot when safe; deferred when live position open |

See `.cursor/rules/edge-profile-maintenance.mdc` for the architectural
overview and the rationale behind each component.

## Why Sundays at 05:00 / 05:30 UTC

* 1h after the ML retrain (04:00 UTC) so the review attributes against
  the freshly-promoted (or held) model
* Same low-activity window as the ML retrain — minimum risk of bot
  restart colliding with active trades
* 30-min offset between review and apply gives the review report time
  to write the JSON sidecar before apply consumes it

## Architecture summary

```
04:00 UTC Sun  retrain_xgb_cron.sh           → maybe promote new ML model
05:00 UTC Sun  edge_profile_review_cron.sh   → JSON sidecar + Discord report
05:30 UTC Sun  edge_profile_apply_cron.sh    → apply Tier 1 (default OFF)
hourly         coordinator._schedule_live_health → tripwire alarms
```

## Weekly checklist (operator action on the Discord report)

When the Sunday Discord report arrives in `#kbtc-attribution`:

### 1. Review the auto-applied changes section first

These are already live. Spot-check each one:

```bash
ssh "$KBTC_DEPLOY_HOST" "docker exec kbtc-db psql -U kalshi -d kbtc -c \
  \"SELECT changed_at, param, old_value, new_value, recommendation_json->'pnl_impact_dollars' \
    FROM edge_profile_change_log \
    WHERE applied_by='auto' AND changed_at > NOW() - INTERVAL '8 days' \
    ORDER BY changed_at DESC\""
```

If a change looks wrong:

```bash
ssh "$KBTC_DEPLOY_HOST" "ls -lt ~/kbtc/.env.backup-auto-* | head -3"
ssh "$KBTC_DEPLOY_HOST" "cp ~/kbtc/.env.backup-auto-<ts> ~/kbtc/.env"
bash scripts/deploy.sh
```

Then add a `EDGE-PROFILE-YYYY-MM-DD` entry to `known-bugs.mdc`
explaining what was rolled back and why.

### 2. Process the manual review section

For each manual rec, verify the underlying numbers in `psql`:

```bash
# Hour-of-day attribution (basis for blocked_hours_utc changes):
ssh "$KBTC_DEPLOY_HOST" "docker exec kbtc-db psql -U kalshi -d kbtc -c \
  \"SELECT EXTRACT(hour FROM timestamp AT TIME ZONE 'UTC')::int AS hr,
           COUNT(*), ROUND(SUM(pnl)::numeric, 2) AS pnl
    FROM trades
    WHERE trading_mode='paper' AND timestamp > NOW() - INTERVAL '14 days'
    GROUP BY hr ORDER BY hr\""

# Direction × conviction (basis for short_min_conviction changes):
ssh "$KBTC_DEPLOY_HOST" "docker exec kbtc-db psql -U kalshi -d kbtc -c \
  \"SELECT direction, conviction, COUNT(*),
           ROUND(SUM(pnl)::numeric, 2) AS pnl,
           ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END)::numeric, 2) AS wr
    FROM trades
    WHERE trading_mode='paper' AND timestamp > NOW() - INTERVAL '14 days'
    GROUP BY direction, conviction ORDER BY direction, conviction\""

# Driver × direction (basis for allowed_drivers changes):
ssh "$KBTC_DEPLOY_HOST" "docker exec kbtc-db psql -U kalshi -d kbtc -c \
  \"SELECT signal_driver, direction, COUNT(*),
           ROUND(SUM(pnl)::numeric, 2) AS pnl
    FROM trades
    WHERE trading_mode='paper' AND timestamp > NOW() - INTERVAL '14 days'
    GROUP BY signal_driver, direction ORDER BY signal_driver, direction\""
```

If the numbers support the recommendation, apply via the sed template
embedded in the Discord post:

```bash
ssh "$KBTC_DEPLOY_HOST" "sed -i 's/^EDGE_LIVE_<KEY>=.*/EDGE_LIVE_<KEY>=<NEW>/' ~/kbtc/.env"
ssh "$KBTC_DEPLOY_HOST" "grep EDGE_LIVE_<KEY> ~/kbtc/.env"   # verify
bash scripts/deploy.sh
```

### 3. Verify post-deploy

```bash
# Live decision should be ENTRY-eligible during a long signal:
ssh "$KBTC_DEPLOY_HOST" "curl -s http://localhost:8000/api/status | python3 -c \
  'import sys,json; d=json.load(sys.stdin); print(json.dumps(d[\"live_decision\"], indent=2))'"

# Confirm the new env values loaded:
ssh "$KBTC_DEPLOY_HOST" "docker exec kbtc-bot env | grep EDGE_LIVE_ | sort"

# Edge profile health snapshot (should show non-elevated skip ratio):
ssh "$KBTC_DEPLOY_HOST" "curl -s http://localhost:8000/api/diagnostics | python3 -c \
  'import sys,json; d=json.load(sys.stdin); print(json.dumps(d[\"edge_profile_health\"], indent=2))'"
```

### 4. Document the change in `known-bugs.mdc`

Add an `EDGE-PROFILE-YYYY-MM-DD` entry citing the changed numbers. This
is what future operators read when wondering why a value is what it is.

## Operator opt-in checklist (first activation of auto-apply)

`EDGE_LIVE_AUTO_APPLY_ENABLED` defaults to `false`. Don't flip it on
until:

- [ ] At least 3 weekly review cycles have produced AUTO_APPLY recs
- [ ] For each cycle's AUTO_APPLY recs, you (the operator) confirm "yes,
      I would have made that change manually"
- [ ] You have an open bookmark to the rollback command:
      `cp ~/kbtc/.env.backup-auto-<ts> ~/kbtc/.env && bash scripts/deploy.sh`
- [ ] You add a `EDGE-PROFILE-AUTOAPPLY-OPT-IN` entry to `known-bugs.mdc`
      documenting the 3-cycle observation period

To activate:

```bash
ssh "$KBTC_DEPLOY_HOST" "sed -i 's/^EDGE_LIVE_AUTO_APPLY_ENABLED=.*/EDGE_LIVE_AUTO_APPLY_ENABLED=true/' ~/kbtc/.env"
ssh "$KBTC_DEPLOY_HOST" "grep EDGE_LIVE_AUTO_APPLY_ENABLED ~/kbtc/.env"
bash scripts/deploy.sh
```

To deactivate (in case of emergency):

```bash
ssh "$KBTC_DEPLOY_HOST" "sed -i 's/^EDGE_LIVE_AUTO_APPLY_ENABLED=.*/EDGE_LIVE_AUTO_APPLY_ENABLED=false/' ~/kbtc/.env"
bash scripts/deploy.sh
```

The next 05:30 UTC apply pass will exit 1 (no-op).

## Tripwire alarms — what to do when one fires

All three tripwires post to `DISCORD_RISK_WEBHOOK`. They share these
properties: cooldown is durable across restarts, fires only in live
mode, no-op in paper mode.

### Live drought alarm

> Live lane has been dark for **48.0h** while paper has executed **23**
> trades in the same 36h window.

What to do:

1. Check `live_decision.skip_reason` in `/api/status` — that names the
   filter doing the rejecting
2. Check the `edge_profile_health` block in `/api/diagnostics` — top
   skip reasons identify the over-blocking gate
3. Run an ad-hoc review:
   `python scripts/edge_profile_review.py --print-only --window-days 7`
4. If a Tier 1 rec exists and you trust it, run apply manually:
   `python scripts/edge_profile_apply.py --recommendations-json <path>`

### EDGE skip-ratio alarm

> **97.4%** of signal_log rows in the last 24h were rejected by an
> EDGE_* filter, for **2** consecutive checks.

What to do:

1. The alarm body lists the top 5 EDGE_* skip reasons. The largest one
   is the gate to investigate.
2. Same investigation flow as the drought alarm.

### Direction-skip imbalance alarm

> Short-side EDGE rejections (**8000**) are **80x** the long-side
> rejections (**100**) over the past 7 days.

What to do:

1. Check the `paper` short attribution for the same 7-day window. If
   paper shorts are profitable while live shorts are 0, the live
   short-side gate (`short_min_price`, `short_min_conviction`) is too
   tight for current regime.
2. Run an ad-hoc review and look at the `EDGE_LIVE_SHORT_MIN_PRICE`
   recommendation. If the suggested floor is *lower* than the current,
   the review will tag MANUAL_ONLY (loosening); apply by hand.

## Cron installation on the droplet

```bash
ssh "$KBTC_DEPLOY_HOST"
crontab -e
```

Add (preserving existing entries):

```cron
0  4 * * 0  /home/botuser/kbtc/scripts/retrain_xgb_cron.sh        >> /home/botuser/kbtc/logs/retrain_xgb.log 2>&1
0  5 * * 0  /home/botuser/kbtc/scripts/edge_profile_review_cron.sh >> /home/botuser/kbtc/logs/edge_review.log 2>&1
30 5 * * 0  /home/botuser/kbtc/scripts/edge_profile_apply_cron.sh  >> /home/botuser/kbtc/logs/edge_apply.log 2>&1
```

Verify after install:

```bash
crontab -l | grep edge_profile
ls -la /home/botuser/kbtc/scripts/edge_profile_*.sh    # should be executable
mkdir -p /home/botuser/kbtc/data/edge_review /home/botuser/kbtc/logs
```

## Migration

Before the first apply run can write to the audit log, the table must
exist on the droplet's DB:

```bash
ssh "$KBTC_DEPLOY_HOST" "docker exec -i kbtc-db psql -U kalshi -d kbtc" \
  < /Users/tyler.shepherd/quant-kbtc/backend/migrations/009_edge_profile_change_log.sql
```

(or rsync the migration file and run it from the remote shell.)

## Smoke tests after first install

```bash
# Manual review run, print only — no Discord, no sidecar
ssh "$KBTC_DEPLOY_HOST" "cd ~/kbtc && docker run --rm --network kbtc_kbtc-net \
  -v \$PWD/backend:/app -v \$PWD/scripts:/scripts \
  -e DATABASE_URL=postgresql://kalshi:kalshi_secret@db:5432/kbtc \
  -w /app --entrypoint python kbtc-bot:latest \
  /scripts/edge_profile_review.py --print-only --window-days 14"

# Manual apply dry-run with the latest sidecar
ssh "$KBTC_DEPLOY_HOST" "ls -1t ~/kbtc/data/edge_review/recommendations_*.json | head -1"
# (use the path above)
ssh "$KBTC_DEPLOY_HOST" "cd ~/kbtc && docker run --rm --network kbtc_kbtc-net \
  -v \$PWD/backend:/app -v \$PWD/scripts:/scripts -v \$PWD/data/edge_review:/recs \
  -v \$PWD/.env:/host/.env \
  -e DATABASE_URL=postgresql://kalshi:kalshi_secret@db:5432/kbtc \
  -w /app --entrypoint python kbtc-bot:latest \
  /scripts/edge_profile_apply.py --recommendations-json /recs/<sidecar.json> \
    --env-file /host/.env --dry-run --no-restart"
```

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Apply exits 1 (no-op) | `EDGE_LIVE_AUTO_APPLY_ENABLED=false` | Expected when operator hasn't opted in |
| Apply exits 2 with "audit insert failed" | DB unreachable or migration not run | `psql` to verify, run migration 009 |
| Apply exits 2 with "restart failed" | Docker daemon issue or compose file path | Inspect `~/kbtc/logs/edge_apply.log`; env was already mutated |
| Review report empty manual section | Recent review already auto-applied everything | No action needed |
| Tripwire firing repeatedly across cooldown | Cooldown state in `bot_state` is stale | `DELETE FROM bot_state WHERE key='live_drought_alarm';` (or skip_ratio/imbalance) |
| Drought alarm firing but live actually trading | `last_live_trade_age_hours` calc bug | Verify `MAX(timestamp) FROM trades WHERE trading_mode='live'` matches dashboard |

## Related

* Rule: `.cursor/rules/edge-profile-maintenance.mdc`
* Code: `backend/monitoring/live_health.py`,
  `scripts/edge_profile_review.py`, `scripts/edge_profile_apply.py`
* Tests: `backend/tests/test_live_health.py`,
  `backend/tests/test_edge_profile_review.py`,
  `backend/tests/test_edge_profile_apply.py`
* ML co-calibration: `docs/runbooks/ml-retraining.md`
* Filter spec: `docs/runbooks/live-edge-filters.md`
