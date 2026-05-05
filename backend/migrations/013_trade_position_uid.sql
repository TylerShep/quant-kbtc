-- Trade ↔ telemetry join key (2026-05-05)
--
-- Background:
--   ``trades.timestamp`` is set to NOW() at trade-close time, so it equals
--   ``trades.closed_at`` exactly. That makes the
--   "join position_telemetry samples taken between entry and exit" pattern
--   resolve to a zero-width window, which silently returns 0 rows.
--
-- Fix:
--   Both ``PaperTrade`` and ``ManagedPosition``/``LiveTrade`` already carry a
--   ``position_uid`` (assigned at entry). Persisting it on the trades row
--   gives us a direct, exact join key against ``position_telemetry``,
--   eliminating the entry-time guesswork and unlocking the
--   exit-intelligence promotion-readiness query.
--
-- Forward-only: existing rows get NULL, which is fine. The join is
-- left-outer on the new column so legacy trades without telemetry are
-- treated as having zero samples — exactly what we want.

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS position_uid VARCHAR(96);

CREATE INDEX IF NOT EXISTS idx_trades_position_uid
    ON trades (position_uid)
    WHERE position_uid IS NOT NULL;
