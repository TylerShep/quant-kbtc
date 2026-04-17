-- migration: historical data enhancement
-- safe to run multiple times (all statements are idempotent)

CREATE TABLE IF NOT EXISTS kalshi_markets (
    ticker           VARCHAR(60)    NOT NULL,
    event_ticker     VARCHAR(60),
    open_time        TIMESTAMPTZ,
    close_time       TIMESTAMPTZ    NOT NULL,
    result           VARCHAR(4),
    expiration_value NUMERIC(14,2),
    last_price       NUMERIC(6,4),
    volume           NUMERIC(20,2),
    open_interest    NUMERIC(20,2),
    source           VARCHAR(20)    NOT NULL DEFAULT 'historical',
    fetched_at       TIMESTAMPTZ    DEFAULT NOW(),
    PRIMARY KEY (ticker)
);
CREATE INDEX IF NOT EXISTS idx_kalshi_markets_close
    ON kalshi_markets (close_time DESC);

CREATE TABLE IF NOT EXISTS kalshi_trades (
    trade_id         VARCHAR(80)    NOT NULL,
    ticker           VARCHAR(60)    NOT NULL,
    count_fp         NUMERIC(14,2),
    yes_price        NUMERIC(6,4),
    taker_side       VARCHAR(4),
    created_time     TIMESTAMPTZ    NOT NULL,
    PRIMARY KEY (trade_id, created_time)
);
SELECT create_hypertable('kalshi_trades', 'created_time',
    chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_kalshi_trades_ticker
    ON kalshi_trades (ticker, created_time DESC);

ALTER TABLE trade_features
    ADD COLUMN IF NOT EXISTS taker_buy_vol  NUMERIC(20,2),
    ADD COLUMN IF NOT EXISTS taker_sell_vol NUMERIC(20,2);

DELETE FROM ob_snapshots a USING ob_snapshots b
    WHERE a.ctid < b.ctid AND a.ticker = b.ticker AND a.timestamp = b.timestamp;

CREATE UNIQUE INDEX IF NOT EXISTS idx_ob_snapshots_ticker_ts
    ON ob_snapshots (ticker, timestamp);
