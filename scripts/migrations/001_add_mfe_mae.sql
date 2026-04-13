-- Migration 001: Add MFE/MAE columns to trade_features
-- Run once against the live TimescaleDB instance.

ALTER TABLE trade_features
  ADD COLUMN IF NOT EXISTS max_favorable_excursion NUMERIC(8,4),
  ADD COLUMN IF NOT EXISTS max_adverse_excursion   NUMERIC(8,4);

COMMENT ON COLUMN trade_features.max_favorable_excursion IS
  'Peak PnL pct the trade ever reached while open (positive = favorable)';

COMMENT ON COLUMN trade_features.max_adverse_excursion IS
  'Worst PnL pct the trade hit while open (negative = adverse drawdown)';
