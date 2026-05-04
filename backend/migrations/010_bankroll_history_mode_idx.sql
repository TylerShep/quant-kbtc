-- 2026-05-04 (BUG-032 follow-up #2)
--
-- /api/equity full-table scan over bankroll_history (1M+ rows) was
-- taking 27+ seconds per call, holding a DB connection that starved
-- every other write task. The cascade pinned bg-persist queue memory
-- and eventually SIGKILLed the bot container in ~2 minutes.
--
-- Composite index on (trading_mode, timestamp DESC) collapses the
-- query plan to an index scan and brings the same query to <50ms.
-- Paired with the bounded `days` parameter on /api/equity in
-- backend/api/routes.py, this completely removed the DB pressure
-- that was driving the post-deploy restart loop.

CREATE INDEX IF NOT EXISTS bankroll_history_mode_ts_idx
    ON bankroll_history (trading_mode, "timestamp" DESC);
