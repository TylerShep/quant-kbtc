-- 011_widen_fill_source.sql
-- Phase 1 (Expiry Exit Reliability, 2026-05-04): widen the trades
-- ``fill_source`` column from VARCHAR(20) to VARCHAR(40) so we can
-- record longer source labels introduced by the realistic paper guard
-- exit work without truncation.
--
-- Existing labels (≤20 chars):
--   'fill_ws', 'fill_ws_partial', 'order_response', 'settlement',
--   'paper_mid_mark'
--
-- New labels (>20 chars):
--   'paper_guard_taker_bidask'  -- realistic taker exit during the
--                                  EXPIRY_GUARD / SHORT_SETTLEMENT_GUARD
--                                  window
--
-- Idempotent: safe to re-run. PostgreSQL ALTER TYPE on VARCHAR
-- only locks rows that need rewriting; for VARCHAR(40) on existing
-- VARCHAR(20) data the change is metadata-only.

ALTER TABLE trades
    ALTER COLUMN fill_source TYPE VARCHAR(40);
