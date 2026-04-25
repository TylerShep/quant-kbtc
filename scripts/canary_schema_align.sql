-- canary_schema_align.sql
-- Bring the canary DB (kbtc_canary) up to schema parity with the main
-- production DB (kbtc) without touching any historical data.
--
-- WHY THIS EXISTS
-- ---------------
-- The bot's startup migration runner (data/historical_sync._run_migration)
-- only auto-applies backend/migrations/002_historical_data.sql. Every
-- migration after 002 (003 spread_state, 004 signal_driver, 004b backfill,
-- 006 pnl reconciliation) has been applied to the main DB by hand in past
-- sessions but never ran against the canary DB, so the canary's schema has
-- silently drifted behind. The drift surfaces as repeated
-- coordinator.persist_signal_failed / persist_trade_failed errors in the
-- canary container logs:
--
--   column "spread_state" of relation "signal_log" does not exist
--   column "signal_driver" of relation "trades"     does not exist
--
-- ...which prevents the canary from writing trade_features rows (because
-- persist_signal fails first), so the canary's contribution to the ML
-- training dataset has been zero since the day each respective column
-- was added to main.
--
-- WHAT THIS SCRIPT DOES
-- ---------------------
-- Replays the same column-additive ALTER + index statements that already
-- live in backend/migrations/{003,004,006}.sql, plus the deterministic
-- conviction-based backfill from 004b. Every statement is idempotent
-- (ADD COLUMN IF NOT EXISTS, CREATE INDEX IF NOT EXISTS, UPDATE WHERE
-- NULL) so re-running this against either DB is a guaranteed no-op.
--
-- WHAT THIS SCRIPT DOES *NOT* DO
-- ------------------------------
-- - No DROP, no DELETE, no TRUNCATE -- existing canary rows are preserved
--   verbatim and only enriched with backfilled signal_driver values where
--   the column is currently NULL.
-- - No schema change to the main DB -- this script targets the canary DB
--   only. Running it against `kbtc` is also safe but pointless (every
--   statement is already present).
-- - No fix to the underlying migration-runner gap. That's a separate
--   follow-up: the bot should iterate over migrations/*.sql in lex order
--   and apply each idempotently, rather than hard-coding 002. Tracked
--   informally; not in scope for this commit.
--
-- HOW TO RUN
-- ----------
--   ssh "$KBTC_DEPLOY_HOST" "docker exec -i kbtc-db-canary \
--       psql -U kalshi -d kbtc_canary -v ON_ERROR_STOP=1" \
--       < scripts/canary_schema_align.sql
--
-- After running, the canary container can be restarted (or just left to
-- continue running -- the next persist_signal call will succeed against
-- the now-aligned schema, no restart required).
--
-- PROVENANCE
-- ----------
--   003_spread_state.sql            -> the spread_state ADD COLUMN below
--   004_trade_signal_driver.sql     -> the two signal_driver ADD COLUMNs
--   004b_backfill_signal_driver.sql -> the conviction-based UPDATEs
--   006_pnl_reconciliation.sql      -> the five trades cost/wallet/fill
--                                       columns + the two indexes

BEGIN;

-- ─── from 003_spread_state.sql ──────────────────────────────────────────
ALTER TABLE signal_log
    ADD COLUMN IF NOT EXISTS spread_state VARCHAR(20);

-- ─── from 004_trade_signal_driver.sql ───────────────────────────────────
ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS signal_driver VARCHAR(32);
ALTER TABLE errored_trades
    ADD COLUMN IF NOT EXISTS signal_driver VARCHAR(32);

-- ─── from 006_pnl_reconciliation.sql (BUG-025) ──────────────────────────
ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS entry_cost_dollars NUMERIC(10,4);
ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS exit_cost_dollars NUMERIC(10,4);
ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS wallet_pnl NUMERIC(14,4);
ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS pnl_drift NUMERIC(10,4);
ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS fill_source VARCHAR(20) DEFAULT 'order_response';

CREATE INDEX IF NOT EXISTS idx_trades_pnl_drift
    ON trades (pnl_drift DESC NULLS LAST)
    WHERE pnl_drift IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_trades_fill_source
    ON trades (fill_source);

-- ─── from 004b_backfill_signal_driver.sql ───────────────────────────────
-- Conviction-based deterministic mapping. Only updates rows where the
-- column is currently NULL (or sentinel values), so re-runs are a no-op.
-- Identical SQL to main's prior backfill so historical canary rows get
-- the same labels they would have if the migration had run on schedule.
UPDATE trades
   SET signal_driver = CASE
        WHEN conviction = 'HIGH'   THEN 'OBI+ROC'
        WHEN conviction = 'NORMAL' THEN 'OBI'
        WHEN conviction = 'LOW'    THEN 'ROC'
        ELSE '-'
   END
 WHERE signal_driver IS NULL
    OR signal_driver = '-'
    OR signal_driver = 'UNKNOWN';

UPDATE errored_trades
   SET signal_driver = CASE
        WHEN conviction = 'HIGH'   THEN 'OBI+ROC'
        WHEN conviction = 'NORMAL' THEN 'OBI'
        WHEN conviction = 'LOW'    THEN 'ROC'
        ELSE '-'
   END
 WHERE signal_driver IS NULL
    OR signal_driver = '-'
    OR signal_driver = 'UNKNOWN';

COMMIT;
