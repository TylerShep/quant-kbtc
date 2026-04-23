# Orphan Safety Promotion Gates

Before unpausing live trading after any change to position management,
reconciliation, or settlement logic, **both** gate sets must pass.

## Gate Set A: Replay Suite (offline, deterministic)

Run: `cd backend && python3 -m pytest tests/replay/ -v`

| # | Gate | Criteria | Covers |
|---|------|----------|--------|
| A1 | Settlement verify failure | No orphan created when verify exhausts retries | Trade 451/454 |
| A2 | Settled ticker persistence | `_settled_tickers` survives snapshot/restore cycle | Restart re-adoption |
| A3 | Reconciliation cooldown | Recently-exited ticker skipped for 90s | Exit race condition |
| A4 | Idempotent adoption | 70+ reconciliation cycles = no count inflation | BUG-015 (423 contracts) |
| A5 | Orphan-to-trade dedup | Duplicate trade within 5min window is skipped | Double-counted PnL |
| A6 | Full lifecycle integration | Enter → settle → restart → reconcile = 0 orphans | End-to-end regression |

**Pass criteria**: 100% of replay tests green (0 failures).

## Gate Set B: Canary Runtime (72-hour demo live-path)

Run: `bash scripts/canary_report.sh`

| # | Gate | Criteria | Query |
|---|------|----------|-------|
| B1 | Canary health | Bot API reachable at :8100 | `GET /api/status` |
| B2 | Orphan settled count | `ORPHAN_SETTLED` trades = 0 | `trades WHERE exit_reason = 'ORPHAN_SETTLED'` |
| B3 | Oversized orphan events | 0 oversized orphan detections | `errored_trades LIKE '%oversized_orphan%'` |
| B4 | No DESYNC state | PositionManager not stuck in DESYNC | `GET /api/status → state` |
| B5 | No duplicate trades | 0 same-ticker trades in same minute | `GROUP BY ticker, minute HAVING COUNT > 1` |
| B6 | Runtime duration | >= 72 hours of canary runtime | `MIN(timestamp)` in trades |
| B7 | Trade activity | At least 1 canary trade executed | `COUNT(*)` in trades |

**Pass criteria**: 0 FAILs. WARNs require operator review and documented justification.

## Promotion Workflow

```
1. Run replay suite     → Gate Set A must pass
2. Deploy canary        → bash scripts/canary_up.sh
3. Wait 72 hours
4. Run canary report    → bash scripts/canary_report.sh
5. If Gate Set B passes → safe to unpause live trading
6. Tear down canary     → bash scripts/canary_down.sh
```

## When to Re-run

Re-run the full promotion workflow after any change to:
- `backend/execution/position_manager.py`
- `backend/coordinator.py` (orphan-related sections)
- `backend/execution/live_trader.py` (entry/exit logic)
- Reconciliation frequency or cooldown parameters
- Settlement handling logic

---

# Strategy Activation Gates

The gate sets above guard the execution path. The gates below guard the
**signal path** — used when activating a new conviction tier or post-resolver
modifier. No new signal may go live-LIVE until it has cleared both.

## Gate Set C: Offline Calibration + Backtest

### C1. Spread distribution sanity (Spread Divergence specific)

Run against the remote DB before merging any SD threshold change:

```bash
ssh "$KBTC_DEPLOY_HOST" "docker exec kbtc-db psql -U kalshi -d kbtc -c \"
SELECT
  percentile_cont(0.10) WITHIN GROUP (ORDER BY spread_cents) AS p10,
  percentile_cont(0.25) WITHIN GROUP (ORDER BY spread_cents) AS p25,
  percentile_cont(0.50) WITHIN GROUP (ORDER BY spread_cents) AS p50,
  percentile_cont(0.75) WITHIN GROUP (ORDER BY spread_cents) AS p75,
  percentile_cont(0.85) WITHIN GROUP (ORDER BY spread_cents) AS p85,
  percentile_cont(0.90) WITHIN GROUP (ORDER BY spread_cents) AS p90,
  percentile_cont(0.95) WITHIN GROUP (ORDER BY spread_cents) AS p95,
  AVG(spread_cents) AS mean,
  STDDEV(spread_cents) AS std,
  COUNT(*) AS n
FROM ob_snapshots
WHERE timestamp > NOW() - INTERVAL '14 days' AND spread_cents IS NOT NULL;\""
```

And the hour-of-day profile:

```bash
ssh "$KBTC_DEPLOY_HOST" "docker exec kbtc-db psql -U kalshi -d kbtc -c \"
SELECT
  EXTRACT(HOUR FROM timestamp) AS hour,
  percentile_cont(0.50) WITHIN GROUP (ORDER BY spread_cents) AS median_spread,
  COUNT(*) AS n
FROM ob_snapshots
WHERE timestamp > NOW() - INTERVAL '14 days' AND spread_cents IS NOT NULL
GROUP BY hour ORDER BY hour;\""
```

**Target**: with defaults (`SD_WIDE=+0.40`, `SD_TIGHT=-0.20`), `WIDE` should
fire for the top 10-15% of MEDIUM-regime readings and `TIGHT` for the bottom
15-20%. If p95/p50 differs significantly from ~1.4 or p10/p50 from ~0.80,
adjust thresholds and re-run.

### C2. ROC-only LOW expectancy backtest (ROC activation specific)

```bash
cd backend && python3 -m backtesting.cli run \
  --from-db --bankroll 1000 --filter-conviction LOW
```

| Metric           | Target         |
|------------------|----------------|
| Trade count (n)  | >= 20          |
| Win rate         | > 52%          |
| Profit factor    | > 1.20 net of fees |

**Pass criteria**: all three or no activation. Record the numbers in the PR description.

## Gate Set D: Paper Runtime (48-hour minimum)

After merging and deploying with `SD_ENABLED=true` and `ROC_LOW_CONVICTION_PAPER_ENABLED=true`:

| # | Gate | Criteria | Query |
|---|------|----------|-------|
| D1 | SD fire rate — WIDE | 10-15% of MEDIUM-regime signals | `SELECT COUNT(*) FILTER (WHERE spread_state='WIDE')::FLOAT / COUNT(*) FROM signal_log WHERE atr_regime='MEDIUM'` |
| D2 | SD fire rate — TIGHT | 15-20% of MEDIUM-regime signals | same, `WHERE spread_state='TIGHT'` |
| D3 | SD `UNKNOWN` absence | <1% of signals (staleness check) | `spread_state IS NULL OR spread_state='UNKNOWN'` |
| D4 | LOW paper trades | >= 10 completed trades at conviction=LOW | `SELECT COUNT(*) FROM trades WHERE conviction='LOW' AND trading_mode='paper'` |
| D5 | LOW paper win rate | > 52% over the 48h window | win_rate on same subset |
| D6 | LOW paper profit factor | > 1.20 net of fees | gross_wins / gross_losses |
| D7 | No divergence vs backtest | live LOW WR within ±5pp of backtest WR | compare |

**Pass criteria**: D1-D3 within band, D4-D7 pass. Only then flip
`ROC_LOW_CONVICTION_LIVE_ENABLED=true`. Live roll-out still requires the
existing Gate Set A (replay) + B (canary, when execution path is touched).

## Rollout Sequence (combined ROC + SD)

```
1. PR opened. Run Gate Set C (C1 + C2) BEFORE review.
2. Merge to main.
3. Apply migration:
     ssh "$KBTC_DEPLOY_HOST" \
       "docker exec kbtc-db psql -U kalshi -d kbtc -f /tmp/003_spread_state.sql"
   (rsync the migration via scripts/deploy.sh first, then run the above)
4. Deploy with:
     SD_ENABLED=true
     ROC_LOW_CONVICTION_PAPER_ENABLED=false
     ROC_LOW_CONVICTION_LIVE_ENABLED=false
5. After 24h: verify D1-D3 (SD distribution sane).
6. Flip ROC_LOW_CONVICTION_PAPER_ENABLED=true, redeploy.
7. After 48h of paper runtime: run Gate Set D. If pass, continue.
8. Flip ROC_LOW_CONVICTION_LIVE_ENABLED=true, redeploy.
9. Monitor first 24h of LIVE LOW trades closely via the Discord trade webhook.
```
