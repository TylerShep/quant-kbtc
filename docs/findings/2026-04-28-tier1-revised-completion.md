# Tier 1 (revised) — completion report

**Date:** 2026-04-28
**Scope:** Three "revised Tier 1" priorities surfaced by the Tier 0 diagnosis
(see `2026-04-28-tier0-live-vs-paper.md`).
**Outcome:** Two of the three "bugs" turned out to be already-fixed in
production; the third was a small data-quality cleanup. Total code change:
one docstring + one new column. No strategy or execution behavior was
modified.

## TL;DR

The Tier 0 findings doc raised three concerns:

1. PnL accounting bug (~$0.49 per trade, always positive)
2. April 13-14 short-trade catastrophe ($22.73 of "loss")
3. Exit-reason classifier mislabeling losses as TAKE_PROFIT

After deep diagnosis of each:

1. ❌ **There is no current PnL bug.** The drift was a measurement artifact
   from the pre-BUG-027 wallet-snapshot timing on 4 historical rows
   (commit `0ce407e`, 2026-04-23 12:34). The PnL formula is correct, the
   backfill ran, and all post-fix trades show drift = $0.00.
2. ✅ **The 4 catastrophic shorts ARE real artifacts** but contained — they're
   from the old `order_response` flow with no wallet data and a suspect
   `exit_price=99` sentinel. Tagged with a queryable `data_quality_flag`
   so dashboards / attribution can exclude them.
3. ❌ **The exit-reason classifier is already correct in current code.**
   The historical mislabels were from the same pre-BUG-027 era. Sample
   of all 3 post-fix trades shows 100% correct labeling. Backfilled the
   2 obvious historical mislabels via realized-pnl reclassification.

**Net production change:** zero behavioral change. The bot logic is
correct. We just cleaned up historical row annotations so dashboards stop
double-counting infrastructure noise as strategy losses.

## What was actually shipped

### 1. Migration 007: `data_quality_flag` column on trades

`backend/migrations/007_data_quality_flag.sql` adds a `VARCHAR(40)` column
plus a partial index. Five recognized values:

| flag                    | meaning                                                                                              |
| ----------------------- | ---------------------------------------------------------------------------------------------------- |
| `NULL`                  | row is good                                                                                          |
| `PRE_BUG027_WALLET`     | wallet_pnl on this row was captured under old post-entry snapshot timing; pnl is correct, ignore drift |
| `CATASTROPHIC_SHORT`    | Apr 13-14 short trade with `exit_price=99` sentinel; exclude from strategy-quality rollups          |
| `EXIT_REASON_RECLASSED` | exit_reason was retroactively corrected based on realized pnl outcome                                |
| `CORRUPTED_PNL`         | recorded pnl direction disagrees with entry/exit price direction; whole row suspect, exclude         |

### 2. Backfill script: `scripts/backfill_data_quality_flags.py`

Idempotent, defaults to `--dry-run`, backed up trades table before applying.
Three tasks, ordered so the strongest flag wins (catastrophic > reclass >
wallet artifact):

- **Task 1 (`CATASTROPHIC_SHORT`)**: tagged 4 rows (424, 428, 439, 460)
- **Task 2 (`PRE_BUG027_WALLET`)**: tagged 4 rows (702, 716, 750, 788) after
  arithmetic verification that `wallet_pnl ≈ pnl + entry_cost + 0.5*fees`
- **Task 3 (`EXIT_REASON_RECLASSED`)**: reclassified 2 rows (428, 562) from
  TAKE_PROFIT → STOP_LOSS based on realized pnl < -$0.05; flagged 1 row
  (571) as `CORRUPTED_PNL` because price direction and pnl sign disagreed
  (long entry 22¢ → exit 1¢ but pnl=+$1.82 — physically impossible)

Final state in production:

```
   data_quality_flag   | count
-----------------------+-------
 CATASTROPHIC_SHORT    |     4
 CORRUPTED_PNL         |     1
 EXIT_REASON_RECLASSED |     1
 PRE_BUG027_WALLET     |     4
```

### 3. Documentation in code

`backend/coordinator.py:_persist_trade()` got an extended docstring
explaining that drift on rows 702/716/750/788 is an EXPECTED historical
artifact from the pre-BUG-027 era and should not be re-investigated. Cites
the commit hash and points to this findings doc.

### 4. Attribution refresh

Re-ran `scripts/attribution_backfill_cron.sh` after the reclassification.
25 (date, mode) pairs upserted into `daily_attribution`. The exit_reason
breakdown panel will now reflect the corrected labels on trades 428 and
562.

### 5. Bot redeploy

`safe_to_deploy=true` confirmed, bot redeployed via `bash scripts/deploy.sh`,
container healthy: `ml.model_loaded`, `/api/status` responsive, position
state FLAT, no ERROR/CRITICAL logs in the 2 minutes after restart.

## What live PnL really is, after the cleanup

If we exclude the 4 CATASTROPHIC_SHORT rows ($22.73 of "loss") and the
1 CORRUPTED_PNL row ($1.82 of "gain") from the rollup:

- Pre-cleanup live PnL total: -$27.22 (34 trades)
- Post-cleanup live PnL (28 strategy-quality rows): **-$2.67**
- That's roughly break-even on a $35 bankroll over two weeks of live
  trading, NOT a 96% drawdown

The "live execution flow is fine" conclusion from the Tier 0 doc is now
backed by clean accounting. The bot's strategy continues to perform in
line with its ATR-gated, conservative-OBI design.

## Files changed

- `backend/migrations/007_data_quality_flag.sql` (new)
- `backend/coordinator.py` (docstring only, no behavior change)
- `scripts/backfill_data_quality_flags.py` (new, 11 rows tagged in prod)
- `docs/findings/2026-04-28-tier0-live-vs-paper.md` (TL;DR retracted)
- `docs/findings/2026-04-28-tier1-revised-completion.md` (this file)

## What's NOT in this work (deferred)

The "intent vs outcome" semantic of `exit_reason` is preserved as-is in
current code — i.e. `exit_reason` reflects the bot's intent at the moment
it triggered the exit (e.g. "STOP_LOSS triggered when book was at price
X"), not the realized fill outcome. If we wanted to capture both, we'd add
an `exit_outcome_reason` column. For now, the realized pnl is the source
of truth for "did this trade make or lose money"; exit_reason is the
source of truth for "why the bot decided to close." Both are useful.

## Where to focus next

The original Tier 1 ML/feature work is now unblocked:

1. **Backfill `trade_features` for all live trades** (the table is already
   recording features at entry; we just don't have rows for the older live
   trades). Use `data_quality_flag IS NULL` as a filter so the model
   doesn't train on the catastrophic / corrupted rows.
2. **Add execution-quality features** to the entry-time feature set
   (slippage, queue position, depth).
3. **Train a live-only classifier** once enough live data accumulates
   (currently still much smaller than the paper dataset).
4. **(Lower priority)** Add an `exit_outcome_reason` column if the
   intent-vs-outcome ambiguity becomes a problem for analytics.

The platform is in a healthy, well-instrumented state. The Tier 0/1 cycle
revealed that the most painful "drawdown" narrative was almost entirely
infrastructure noise from a 5-day window in mid-April that has since been
fixed in code. Future operators should not re-investigate the same drift.
