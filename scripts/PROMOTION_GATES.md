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
