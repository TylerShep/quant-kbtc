-- Exit intelligence telemetry (2026-05-04)
-- Adds sampled open-position telemetry used by:
--   1) MFE/MAE trajectory analysis
--   2) Intra-candle momentum diagnostics
--   3) Position health-score observability

CREATE TABLE IF NOT EXISTS position_telemetry (
    id                  BIGSERIAL       NOT NULL,
    timestamp           TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    position_uid        VARCHAR(96)     NOT NULL,
    trading_mode        VARCHAR(10)     NOT NULL,
    ticker              VARCHAR(60)     NOT NULL,
    direction           VARCHAR(10)     NOT NULL,
    mark_price          NUMERIC(10,4),
    unrealized_pnl_pct  NUMERIC(10,6),
    mfe_pct             NUMERIC(10,6),
    mae_pct             NUMERIC(10,6),
    health_score        NUMERIC(6,2),
    health_breach_count INTEGER,
    obi                 NUMERIC(8,4),
    roc_15m             NUMERIC(10,4),
    mini_roc_fast       NUMERIC(10,6),
    mini_roc_slow       NUMERIC(10,6),
    atr_regime          VARCHAR(12),
    time_remaining_sec  INTEGER,
    spot_price          NUMERIC(14,4),
    health_components   JSONB,
    PRIMARY KEY (timestamp, id)
);

-- If a partial prior run created the table with ``PRIMARY KEY (id)``, fix it
-- before hypertable conversion (Timescale requires partition key inclusion).
ALTER TABLE position_telemetry DROP CONSTRAINT IF EXISTS position_telemetry_pkey;
ALTER TABLE position_telemetry
    ADD CONSTRAINT position_telemetry_pkey PRIMARY KEY (timestamp, id);

SELECT create_hypertable(
    'position_telemetry',
    'timestamp',
    chunk_time_interval => INTERVAL '1 day',
    migrate_data => TRUE,
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_position_telemetry_uid_ts
    ON position_telemetry (position_uid, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_position_telemetry_mode_ts
    ON position_telemetry (trading_mode, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_position_telemetry_ticker_ts
    ON position_telemetry (ticker, timestamp DESC);

ALTER TABLE position_telemetry
    SET (timescaledb.compress, timescaledb.compress_segmentby = 'position_uid,trading_mode');
SELECT add_compression_policy('position_telemetry', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_retention_policy('position_telemetry', INTERVAL '90 days', if_not_exists => TRUE);
