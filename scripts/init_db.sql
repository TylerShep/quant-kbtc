CREATE EXTENSION IF NOT EXISTS timescaledb;

-- 15-minute OHLCV candles from all sources
CREATE TABLE IF NOT EXISTS candles (
    timestamp       TIMESTAMPTZ     NOT NULL,
    source          VARCHAR(20)     NOT NULL,
    symbol          VARCHAR(20)     NOT NULL,
    open            NUMERIC(14,4)   NOT NULL,
    high            NUMERIC(14,4)   NOT NULL,
    low             NUMERIC(14,4)   NOT NULL,
    close           NUMERIC(14,4)   NOT NULL,
    volume          NUMERIC(22,6)   NOT NULL,
    PRIMARY KEY (timestamp, source, symbol)
);
SELECT create_hypertable('candles', 'timestamp', chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_candles_symbol ON candles (symbol, source, timestamp DESC);

-- Order book snapshots (every 30 seconds)
CREATE TABLE IF NOT EXISTS ob_snapshots (
    timestamp       TIMESTAMPTZ     NOT NULL,
    ticker          VARCHAR(60)     NOT NULL,
    bids            JSONB           NOT NULL,
    asks            JSONB           NOT NULL,
    obi             NUMERIC(6,4),
    total_bid_vol   NUMERIC(20,2),
    total_ask_vol   NUMERIC(20,2),
    spread_cents    SMALLINT
);
SELECT create_hypertable('ob_snapshots', 'timestamp', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_ob_ticker ON ob_snapshots (ticker, timestamp DESC);

-- Strategy signal log
CREATE TABLE IF NOT EXISTS signal_log (
    timestamp       TIMESTAMPTZ     NOT NULL,
    ticker          VARCHAR(60),
    obi_value       NUMERIC(6,4),
    obi_direction   VARCHAR(20),
    roc_value       NUMERIC(10,4),
    roc_direction   VARCHAR(20),
    atr_regime      VARCHAR(20),
    decision        VARCHAR(40),
    conviction      VARCHAR(20),
    skip_reason     VARCHAR(60),
    size_mult       NUMERIC(4,2)
);
SELECT create_hypertable('signal_log', 'timestamp', chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_signal_ticker ON signal_log (ticker, timestamp DESC);

-- Trade records
CREATE TABLE IF NOT EXISTS trades (
    id              BIGSERIAL,
    timestamp       TIMESTAMPTZ     NOT NULL,
    ticker          VARCHAR(60)     NOT NULL,
    direction       VARCHAR(10)     NOT NULL,
    side            VARCHAR(10)     NOT NULL,
    contracts       INTEGER         NOT NULL,
    entry_price     NUMERIC(10,4)   NOT NULL,
    exit_price      NUMERIC(10,4),
    pnl             NUMERIC(14,4),
    pnl_pct         NUMERIC(8,4),
    fees            NUMERIC(10,4),
    exit_reason     VARCHAR(40),
    conviction      VARCHAR(10),
    regime_at_entry VARCHAR(10),
    candles_held    INTEGER,
    entry_obi       NUMERIC(6,4),
    entry_roc       NUMERIC(10,4),
    closed_at       TIMESTAMPTZ,
    trading_mode    VARCHAR(10)     NOT NULL DEFAULT 'paper'
);
SELECT create_hypertable('trades', 'timestamp', chunk_time_interval => INTERVAL '30 days', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades (ticker, timestamp DESC);

-- Bankroll history
CREATE TABLE IF NOT EXISTS bankroll_history (
    timestamp       TIMESTAMPTZ     NOT NULL,
    bankroll        NUMERIC(14,4)   NOT NULL,
    peak_bankroll   NUMERIC(14,4)   NOT NULL,
    drawdown_pct    NUMERIC(8,4),
    daily_pnl       NUMERIC(14,4),
    trade_count     INTEGER,
    trading_mode    VARCHAR(10)     NOT NULL DEFAULT 'paper'
);
SELECT create_hypertable('bankroll_history', 'timestamp', chunk_time_interval => INTERVAL '30 days', if_not_exists => TRUE);

-- Latency metrics
CREATE TABLE IF NOT EXISTS latency_metrics (
    timestamp       TIMESTAMPTZ     NOT NULL,
    operation       VARCHAR(40)     NOT NULL,
    elapsed_ms      NUMERIC(10,3)   NOT NULL,
    breach          BOOLEAN         DEFAULT FALSE
);
SELECT create_hypertable('latency_metrics', 'timestamp', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);

-- Pipeline health
CREATE TABLE IF NOT EXISTS pipeline_health (
    timestamp           TIMESTAMPTZ     NOT NULL,
    source              VARCHAR(20)     NOT NULL,
    lag_seconds         NUMERIC(8,2),
    candle_gaps         INTEGER         DEFAULT 0,
    ob_snapshot_count   INTEGER         DEFAULT 0,
    validation_errors   INTEGER         DEFAULT 0
);
SELECT create_hypertable('pipeline_health', 'timestamp', chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);

-- Errored trades (quarantined from main trades table)
CREATE TABLE IF NOT EXISTS errored_trades (LIKE trades INCLUDING ALL);
ALTER TABLE errored_trades ADD COLUMN IF NOT EXISTS error_reason VARCHAR(100);
ALTER TABLE errored_trades ADD COLUMN IF NOT EXISTS flagged_at TIMESTAMPTZ DEFAULT NOW();

-- Parameter tuning recommendations
CREATE TABLE IF NOT EXISTS param_recommendations (
    timestamp           TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    current_params      JSONB           NOT NULL,
    recommended_params  JSONB           NOT NULL,
    edge_consistency    NUMERIC(6,4),
    avg_oos_sharpe      NUMERIC(8,4),
    should_apply        BOOLEAN         DEFAULT FALSE,
    reason              VARCHAR(200),
    changes             JSONB
);
SELECT create_hypertable('param_recommendations', 'timestamp', chunk_time_interval => INTERVAL '30 days', if_not_exists => TRUE);

-- Daily attribution snapshots (one row per calendar day)
CREATE TABLE IF NOT EXISTS daily_attribution (
    date            DATE            NOT NULL PRIMARY KEY,
    total_trades    INTEGER         NOT NULL,
    total_pnl       NUMERIC(14,4)   NOT NULL,
    attribution     JSONB           NOT NULL,
    trading_mode    VARCHAR(10)     NOT NULL DEFAULT 'paper',
    created_at      TIMESTAMPTZ     DEFAULT NOW()
);

-- Bot state (key-value for heartbeat, bankroll persistence, etc.)
CREATE TABLE IF NOT EXISTS bot_state (
    key             VARCHAR(60)     PRIMARY KEY,
    value           JSONB           NOT NULL,
    updated_at      TIMESTAMPTZ     DEFAULT NOW()
);

-- Enable compression on hypertables then add policies
ALTER TABLE ob_snapshots SET (timescaledb.compress, timescaledb.compress_segmentby = 'ticker');
SELECT add_compression_policy('ob_snapshots', INTERVAL '7 days', if_not_exists => TRUE);

ALTER TABLE latency_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'operation');
SELECT add_compression_policy('latency_metrics', INTERVAL '3 days', if_not_exists => TRUE);

ALTER TABLE pipeline_health SET (timescaledb.compress, timescaledb.compress_segmentby = 'source');
SELECT add_compression_policy('pipeline_health', INTERVAL '14 days', if_not_exists => TRUE);

-- Retention policies (latency and pipeline health kept 90 days)
SELECT add_retention_policy('latency_metrics', INTERVAL '90 days', if_not_exists => TRUE);
SELECT add_retention_policy('pipeline_health', INTERVAL '90 days', if_not_exists => TRUE);
