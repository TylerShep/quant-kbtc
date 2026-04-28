-- 008_execution_quality_features.sql
-- Tier 1.b (2026-04-28): add four execution-quality features to trade_features
-- so the next ML model generation (xgb_entry_v2) can learn from them.
--
-- Why these four:
--   The current 14 features (obi, roc_*, atr_pct, spread_pct, etc.) describe
--   MARKET STATE — "what is the world like right now?" — but say nothing
--   about EXECUTION QUALITY at the price we're about to trade at. The 24
--   live trades + the SHORT_SETTLEMENT_GUARD analysis from 2026-04-28 made
--   it obvious that execution quality is the dominant driver of live-vs-paper
--   performance gaps. These four features capture the missing signal:
--
--   * minutes_to_contract_close   — proximity to the gamma-blow-up window.
--                                    Direct evidence: shorts with <13 min were
--                                    0% WR / -$6.6k net across 27 trades.
--   * quoted_spread_at_entry_bps  — how wide the book is RIGHT NOW. Spread
--                                    in cents alone is misleading because
--                                    a 2c spread on a 10c contract (2000 bps)
--                                    is very different from a 2c spread on a
--                                    50c contract (400 bps).
--   * book_thickness_at_offer     — total contracts available within ±5c of
--                                    mid price. Captures the "how much can
--                                    we actually fill" question. Thin books
--                                    in the close window were a systematic
--                                    contributor to bad short fills.
--   * recent_trade_count_60s      — proxy for "is the contract being actively
--                                    traded right now?". Captures liquidity
--                                    that a static depth snapshot misses.
--
-- Idempotent: safe to re-run.
--
-- After applying this migration, you must:
--   1. Restart the bot (so extract_features() starts populating the new cols)
--   2. Wait at least 7 days for the new features to populate enough paper
--      rows (~200+) before retraining xgb_entry to v2
--   3. Retrain via scripts/retrain_xgb_cron.sh (will pick up new ENTRY_FEATURES
--      automatically once it's promoted to v2 in train_xgb.py)

ALTER TABLE trade_features
    ADD COLUMN IF NOT EXISTS minutes_to_contract_close   NUMERIC(8,3);

ALTER TABLE trade_features
    ADD COLUMN IF NOT EXISTS quoted_spread_at_entry_bps  INTEGER;

ALTER TABLE trade_features
    ADD COLUMN IF NOT EXISTS book_thickness_at_offer     NUMERIC(20,2);

ALTER TABLE trade_features
    ADD COLUMN IF NOT EXISTS recent_trade_count_60s      INTEGER;
