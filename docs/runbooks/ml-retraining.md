# ML entry-gate retraining runbook

Weekly automated retrain of the XGBoost entry-gate classifier
(`backend/ml/models/xgb_entry_v1.pkl`), with a promotion gate that blocks
candidates that regress against the current incumbent.

| | |
|---|---|
| Cron schedule | `0 4 * * 0` — Sunday 04:00 UTC |
| Trigger | `/home/botuser/kbtc/scripts/retrain_xgb_cron.sh` |
| Notification | Discord errors webhook (success, hold, and failure all post) |
| Promotion | Atomic file swap; archive of incumbent kept in `backend/ml/models/archive/` |
| Bot restart | **Manual.** Cron does NOT restart the bot. Operator decides when to load a new model. |

## Why Sunday 04:00 UTC

- Lowest BTC volume of the week (post weekend close, before Asia open Monday)
- Lowest bot trading activity → smallest `safe_to_deploy` rejection probability when we eventually restart
- Avoids overlap with the daily canary report (09:00 UTC) and DB backups (03:30 UTC)

## Architecture

```
cron @ 04:00 UTC Sun
  └─ scripts/retrain_xgb_cron.sh
       ├─ docker exec kbtc-db pg_dump trade_features  →  data/retrain/trade_features_<ts>.csv
       ├─ row count check (>= 200)
       ├─ docker run --rm --user 1000:1000 kbtc-bot:latest python /scripts/retrain_promote.py
       │    ├─ scripts/train_xgb.py train()                  # 5-fold CV, threshold tuned on PR curve
       │    ├─ promotion gate (rows / abs floor / regression tolerance)
       │    ├─ archive incumbent → backend/ml/models/archive/xgb_entry_v1_<ts>.pkl
       │    └─ atomic replace → backend/ml/models/xgb_entry_v1.pkl
       └─ post outcome to Discord errors webhook
```

## Promotion gate

A retrained candidate is only promoted if **all** of these pass:

| Check | Threshold | Rationale |
|---|---|---|
| Labeled rows | >= 200 | Smaller samples produce CV precision estimates with SE > 0.05 |
| Candidate row count | >= 95% of incumbent rows | Catches accidental data wipes |
| Candidate OOS precision | >= 0.58 | Matches the absolute floor in `train_xgb.py`; below this the signal isn't strong enough to gate live trades |
| Candidate vs incumbent precision | candidate_precision + 0.10 >= incumbent_precision | Allows noise & cross-platform variance, blocks clear regressions |

**On hold (gate fails):** the candidate `.pkl` is deleted, incumbent untouched,
script exits 2, Discord alert posted with a 🟡 emoji.

**On promote:** incumbent is copied to `backend/ml/models/archive/xgb_entry_v1_<ts>.pkl`,
then the candidate atomically replaces `xgb_entry_v1.pkl` and `xgb_entry_v1_meta.json`.
Script exits 0, Discord alert posted with ✅ and an "action required" reminder
that a bot restart is needed to actually load the new model.

## Why the regression tolerance is so wide (0.10)

The 0.10 tolerance looks loose, but it's correct for our setup:

1. **Cross-platform XGBoost variance.** Training the same data with the same
   hyperparameters on Mac (numpy 2.0, py3.9) vs Linux (numpy 2.1, py3.11)
   produces models with OOS precision differing by ~0.05. We standardize on
   container training to minimize this, but residual variance remains.
2. **Sampling noise.** With N ≈ 500 and 5-fold CV, the precision estimate has
   a standard error around 0.02–0.03. A delta of -0.05 between consecutive
   weekly retrains is well within sampling noise even on identical signal
   strength.
3. **Conservative guardrails elsewhere.** The promoted model still has to clear
   the absolute precision floor (0.58), and a human still has to manually
   restart the bot to load it. This is not a fully autonomous loop.

When sample size grows past ~2000 labeled rows, tighten the tolerance to 0.05.

## Operator actions

### Reading a Discord notification

| Emoji | Meaning | Action |
|---|---|---|
| ✅ PROMOTED | Candidate cleared the gate, swap completed | Verify the metrics look reasonable; restart the bot at the next safe window to load it |
| 🟡 HELD | Candidate did not improve on incumbent | Read the `PROMOTION DECISION` block to see which check failed; usually no action needed |
| ❌ ERRORED | Script crashed (DB unreachable, training error, IO failure) | Check `/home/botuser/kbtc/logs/retrain_xgb.log` and triage |
| ℹ️ DRY RUN | Manual `--dry-run` invocation finished | No action |

### Loading a promoted model

A bot restart is required to load a new model — the model is read once at
startup. Wait for `safe_to_deploy: true`, then:

```bash
ssh "$KBTC_DEPLOY_HOST"
cd /home/botuser/kbtc
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-build bot
```

Then verify the load:

```bash
docker logs --since 60s kbtc-bot | grep ml.model_loaded
# Expected: {"path": "/app/ml/models/xgb_entry_v1.pkl", "features": 14, "threshold": ...}
```

### Manual run / dry run

Test the full pipeline without promoting:

```bash
ssh "$KBTC_DEPLOY_HOST"
/home/botuser/kbtc/scripts/retrain_xgb_cron.sh --dry-run
```

Force a real run outside the cron schedule (e.g. after a bulk data import):

```bash
ssh "$KBTC_DEPLOY_HOST"
/home/botuser/kbtc/scripts/retrain_xgb_cron.sh
```

### Rolling back a bad promotion

If a promoted model behaves badly in production and you want to revert:

```bash
ssh "$KBTC_DEPLOY_HOST"
cd /home/botuser/kbtc/backend/ml/models

# Find the most recent archived model (the one that was incumbent before the bad promotion)
ls -t archive/

# Restore it
cp archive/xgb_entry_v1_<ts>.pkl xgb_entry_v1.pkl
cp archive/xgb_entry_v1_<ts>_meta.json xgb_entry_v1_meta.json

# Restart bot to load the rolled-back model
cd /home/botuser/kbtc
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-build bot
```

## File layout

```
backend/ml/models/
├── xgb_entry_v1.pkl              # active model (loaded at bot startup)
├── xgb_entry_v1_meta.json        # active metadata (precision, threshold, features, importances)
├── .promotion_log.json           # last 50 promotion decisions (audit trail)
└── archive/
    ├── xgb_entry_v1_<ts>.pkl     # historical incumbent at time of each promotion
    └── xgb_entry_v1_<ts>_meta.json
```

The archive is unbounded (each weekly run that promotes adds 2 files). At
~289 KB per `.pkl`, even 100 weeks of archives is ~30 MB — no pruning needed
for now. If it gets unwieldy, prune entries older than 6 months.

## Known limitations

- **No live-mode auto-promotion.** A promoted model only affects paper trading
  until you flip `ML_GATE_LIVE=true` manually in `.env`. This is intentional.
- **No drift detection.** The script does not check feature drift between the
  candidate and incumbent training distributions. A future improvement would
  be a KS test on each feature; for now, the precision regression check is
  the main signal that something has changed.
- **Cross-platform model variance.** As above. Standardize all training in the
  container (cron does this; ad-hoc local training does not).
- **Bot restart is manual.** This avoids automated restarts colliding with
  open positions, but it means a promoted model can sit idle for days if the
  operator doesn't notice the Discord alert. Monitor the channel.

## Related files

- `scripts/retrain_xgb_cron.sh` — orchestrator (cron entrypoint)
- `scripts/retrain_promote.py` — train + compare + promote logic
- `scripts/train_xgb.py` — underlying training code (also used for first-time training)
- `backend/ml/inference.py` — runtime model loader; bot calls `load_model()` once at startup
- `backend/config/settings.py` — `MLConfig` reads `ML_GATE_ENABLED` / `ML_GATE_PAPER` / `ML_GATE_LIVE`
