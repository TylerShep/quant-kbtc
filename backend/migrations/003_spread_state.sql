-- 003_spread_state.sql
-- Add spread_state column to signal_log for Spread Divergence modifier logging.
-- Idempotent: safe to re-run.

ALTER TABLE signal_log ADD COLUMN IF NOT EXISTS spread_state VARCHAR(20);
