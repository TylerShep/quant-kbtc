-- Expiry Exit Reliability — post-deploy verification queries.
--
-- Run on the production DB after Phase 1/2/3 rollout to confirm the
-- changes behave as designed. Each query has a "before" expectation
-- (from the pre-rollout baseline already known in the chat history)
-- and a "watch for" criterion to catch regressions.
--
-- Usage on the remote droplet:
--   ssh "$KBTC_DEPLOY_HOST" "docker exec -i kbtc-db psql -U kalshi -d kbtc" \
--     < scripts/expiry_exit_reliability_verify.sql
--
-- Per-section comments include rollout rubrics; do not edit results in
-- place — capture the output to a dated text file under
-- backend/backtest_reports/.

\echo
\echo === Phase 1: paper guard fill_source distribution ===
\echo Expectation after deploy: new fills land with paper_guard_taker_bidask
\echo (executable bid/ask) when liquidity exists, paper_mid_mark only for
\echo non-guard exits or pre-deploy data. Should NOT see paper_mid_mark on
\echo any new EXPIRY_GUARD / SHORT_SETTLEMENT_GUARD rows.
SELECT
    exit_reason,
    fill_source,
    COUNT(*) AS n,
    ROUND(AVG(pnl)::numeric, 2) AS avg_pnl,
    ROUND(SUM(pnl)::numeric, 2) AS total_pnl
FROM trades
WHERE trading_mode = 'paper'
  AND exit_reason IN ('EXPIRY_GUARD', 'SHORT_SETTLEMENT_GUARD')
  AND timestamp >= NOW() - INTERVAL '7 days'
GROUP BY exit_reason, fill_source
ORDER BY exit_reason, fill_source;

\echo
\echo === Phase 1: paper guard win-rate sanity ===
\echo Pre-deploy paper EXPIRY_GUARD win rate was inflated to ~91% via
\echo synthetic mid fills. Post-deploy expect realistic win rate closer
\echo to live, generally 55-70%.
SELECT
    exit_reason,
    COUNT(*) AS n,
    ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) AS win_pct,
    ROUND(AVG(pnl)::numeric, 2) AS avg_pnl
FROM trades
WHERE trading_mode = 'paper'
  AND exit_reason IN ('EXPIRY_GUARD', 'SHORT_SETTLEMENT_GUARD')
  AND timestamp >= NOW() - INTERVAL '7 days'
GROUP BY exit_reason
ORDER BY exit_reason;

\echo
\echo === Phase 2: live EXPIRY_GUARD outcome distribution ===
\echo Watch for: ratio of EXPIRY_GUARD success vs ORPHAN_SETTLED /
\echo EXPIRY_409_SETTLED should improve once retry widening defaults are
\echo flipped on (EXPIRY_RETRY_WIDEN_STEP_CENTS > 0). Until then this is
\echo a baseline.
SELECT
    exit_reason,
    COUNT(*) AS n,
    ROUND(AVG(pnl)::numeric, 2) AS avg_pnl
FROM trades
WHERE trading_mode = 'live'
  AND exit_reason IN ('EXPIRY_GUARD', 'SHORT_SETTLEMENT_GUARD',
                      'ORPHAN_SETTLED', 'EXPIRY_409_SETTLED',
                      'CONTRACT_SETTLED')
  AND timestamp >= NOW() - INTERVAL '7 days'
GROUP BY exit_reason
ORDER BY exit_reason;

\echo
\echo === Phase 3: confirm ladder is dormant (default off) ===
\echo Until LADDER_ENABLED_LIVE=true, no fill_source should be
\echo paper_ladder_fill or live_ladder_fill. If counts are non-zero
\echo without an explicit operator change, investigate.
SELECT
    fill_source,
    COUNT(*) AS n
FROM trades
WHERE timestamp >= NOW() - INTERVAL '7 days'
  AND fill_source LIKE '%ladder%'
GROUP BY fill_source
ORDER BY fill_source;

\echo
\echo === Sanity: schema migration applied ===
\echo Expect data_type=character varying, character_maximum_length=40.
SELECT
    column_name, data_type, character_maximum_length
FROM information_schema.columns
WHERE table_name = 'trades' AND column_name = 'fill_source';
