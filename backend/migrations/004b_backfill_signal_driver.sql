-- 004b_backfill_signal_driver.sql
-- One-off backfill: populate signal_driver for trades that predate the
-- attribution column.
--
-- METHOD: Conviction-based deterministic mapping.
--
-- The signal-conflict resolver's COORDINATION_TABLE is the *only* place
-- conviction levels are assigned for non-orphan trades, and the mapping
-- is deterministic for historical (pre-Spread-Divergence, pre-LOW-live)
-- trades:
--
--     conviction = HIGH    => OBI fired AND ROC fired (and agreed)
--                          => signal_driver = 'OBI+ROC'
--
--     conviction = NORMAL  => OBI fired, ROC neutral
--                          => signal_driver = 'OBI'
--
--     conviction = LOW     => ROC fired, OBI neutral
--                          => signal_driver = 'ROC'
--
--     conviction = UNKNOWN => orphan-recovered trade, unknowable
--                          => signal_driver = '-'
--
-- We deliberately use conviction instead of the stored entry_obi /
-- entry_roc snapshots because those values were captured at order-placement
-- time (after sizing, price-guard, etc.) by which point the signals had
-- often decayed below their firing thresholds. The conviction column,
-- however, was assigned by the resolver at decision time and accurately
-- reflects which signals fired.
--
-- NO Spread Divergence suffix: SD wasn't running for these trades.
--
-- Idempotent: only updates rows where signal_driver IS NULL, '-', or 'UNKNOWN'.
-- Re-running has no effect on already-tagged rows.

BEGIN;

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
