-- 005_fix_daily_attribution_pk.sql
-- Fix daily_attribution primary key from (date) to (date, trading_mode)
-- so the coordinator's `ON CONFLICT (date, trading_mode)` upsert works.
-- Without this, the daily attribution job silently fails and no rows are written.
-- Idempotent: safe to re-run.

ALTER TABLE daily_attribution DROP CONSTRAINT IF EXISTS daily_attribution_pkey;
ALTER TABLE daily_attribution ADD CONSTRAINT daily_attribution_pkey PRIMARY KEY (date, trading_mode);
