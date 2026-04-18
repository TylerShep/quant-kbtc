-- 004_trade_signal_driver.sql
-- Add signal_driver column to trades and errored_trades for attribution.
-- Stores a short human-readable label of which signals drove the trade
-- (e.g. 'OBI+ROC', 'OBI', 'ROC', 'OBI+ROC/TIGHT'). Does not affect any
-- trading logic, only reporting and analysis.
-- Idempotent: safe to re-run.

ALTER TABLE trades         ADD COLUMN IF NOT EXISTS signal_driver VARCHAR(32);
ALTER TABLE errored_trades ADD COLUMN IF NOT EXISTS signal_driver VARCHAR(32);
