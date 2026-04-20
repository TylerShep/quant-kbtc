-- 006_pnl_reconciliation.sql
-- BUG-025: add per-trade reconciliation columns so we can quantify
-- drift between the recorded cost-based PnL and the actual wallet
-- delta, and tell apart trades whose prices/costs came from the
-- authenticated `fill` WebSocket vs. the legacy polled order response.
--
-- Idempotent: safe to re-run.
--
-- Columns:
--   entry_cost_dollars  Dollar amount Kalshi charged on entry. Sourced
--                       from the WS Fill events when available, else
--                       from the order response's taker_fill_cost_dollars.
--   exit_cost_dollars   Same, for the exit leg. NULL for settlement-
--                       closed trades (no exit order).
--   wallet_pnl          Post-exit wallet balance minus pre-entry wallet
--                       balance, in dollars. NULL for paper trades or
--                       when wallet capture failed.
--   pnl_drift           abs(pnl - wallet_pnl). Drift > $0.05 also lands
--                       a row in errored_trades so it's easy to spot.
--   fill_source         How the prices were obtained for this trade:
--                       'fill_ws'         -- exit (or entry, if no exit)
--                                            had a complete WS drain
--                       'fill_ws_partial' -- WS drain returned fewer
--                                            executions than expected
--                       'order_response'  -- legacy parsed-poll path
--                       'settlement'      -- contract settled (no exit
--                                            fills exist for this trade)

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
