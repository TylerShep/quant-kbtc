-- Edge profile change log: append-only audit of every edge_profile env
-- mutation, whether applied automatically by scripts/edge_profile_apply.py
-- or manually by an operator following the Discord recommendation.
--
-- Used by:
--   * scripts/edge_profile_apply.py  -- per-param 7-day throttle + audit
--   * scripts/edge_profile_review.py -- "last changed: N days ago" context
--   * GET /api/diagnostics           -- recent_auto_changes panel
--
-- Idempotent.

CREATE TABLE IF NOT EXISTS edge_profile_change_log (
    id BIGSERIAL PRIMARY KEY,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    param VARCHAR(64) NOT NULL,
    old_value TEXT NOT NULL,
    new_value TEXT NOT NULL,
    recommendation_json JSONB,
    applied_by VARCHAR(16) NOT NULL CHECK (applied_by IN ('auto', 'manual')),
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_edge_change_log_param
    ON edge_profile_change_log (param, changed_at DESC);

CREATE INDEX IF NOT EXISTS idx_edge_change_log_changed_at
    ON edge_profile_change_log (changed_at DESC);
