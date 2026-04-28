-- 007_data_quality_flag.sql
-- Tier 1 data integrity work (2026-04-28): tag historical trade rows whose
-- recorded fields are infrastructure artifacts rather than real strategy
-- outcomes, so dashboards / attribution / ML labeling can filter them out
-- without losing the row entirely.
--
-- Why a separate column instead of mutating exit_reason in-place:
--   * exit_reason still represents the bot's intent at the time of exit
--     (e.g. "STOP_LOSS triggered when book was at price X"). That signal
--     has analytical value even when the realized fill was different.
--   * data_quality_flag is the *outcome* annotation -- it answers "is
--     this row safe to use in a strategy-PnL rollup?".
--   * Keeping both lets dashboards and ML pipelines pick the right field
--     without a second migration.
--
-- Idempotent: safe to re-run.
--
-- Recognized flag values (string enum, no DB-level constraint to keep
-- additions lightweight; backfill scripts add new tags with a comment):
--   NULL                     -- default; row is good
--   'PRE_BUG027_WALLET'      -- wallet_pnl on this row was captured under
--                               the old post-entry snapshot timing and is
--                               not directly comparable to pnl. The pnl
--                               value itself is correct (already backfilled
--                               by scripts/backfill_pnl_bug027.py).
--   'CATASTROPHIC_SHORT'     -- Apr 13-14 short trades whose recorded
--                               exit_price=99 looks like a settlement-time
--                               artifact, not a real sell at 99c. We have
--                               no wallet data to verify. Exclude from
--                               strategy-quality rollups.
--   'EXIT_REASON_RECLASSED'  -- exit_reason was retroactively corrected
--                               based on realized pnl outcome (e.g. a
--                               TAKE_PROFIT label that resulted in a
--                               $5.70 loss got renamed STOP_LOSS).
--   'CORRUPTED_PNL'          -- recorded pnl direction disagrees with the
--                               entry/exit price direction (e.g. a long
--                               that exited far below entry but recorded
--                               a positive pnl). The whole row is suspect
--                               -- exclude from analytics, do not trust
--                               pnl OR exit_reason. Apply only after
--                               manual investigation; the backfill script
--                               adds this flag automatically when it
--                               detects the inconsistency.

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS data_quality_flag VARCHAR(40);

CREATE INDEX IF NOT EXISTS idx_trades_data_quality_flag
    ON trades (data_quality_flag)
    WHERE data_quality_flag IS NOT NULL;
