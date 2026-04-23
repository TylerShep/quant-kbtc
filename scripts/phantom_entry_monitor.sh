#!/usr/bin/env bash
set -euo pipefail

# BUG-022 phantom-entry monitor.
#
# Counts the entry-pipeline events relevant to BUG-022 (phantom_entry race)
# from the live bot's docker logs and from the trades table. Re-run anytime
# to compare before/after deploys, or schedule via cron.
#
# Usage: bash scripts/phantom_entry_monitor.sh [user@host]

REMOTE="${1:-${KBTC_DEPLOY_HOST:-deploy@your-host}}"

echo "=== BUG-022 Phantom-Entry Monitor ==="
echo "Remote: ${REMOTE}"
echo "Run at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

echo "── Entry pipeline events (docker logs window) ──"
ssh "${REMOTE}" "docker logs kbtc-bot 2>&1 | awk '
    /position_manager.order_placed/        { placed++ }
    /position_manager.poll_order_early_bailout/ { bailout++ }
    /position_manager.entry_canceled_on_timeout/ { cancel_ok++ }
    /position_manager.entry_cancel_already_terminal/ { cancel_404++ }
    /position_manager.entry_cancel_failed/ { cancel_err++ }
    /position_manager.entry_filled_after_cancel/ { filled_after_cancel++ }
    /position_manager.entry_poll_disagrees_exchange/ { disagree++ }
    /position_manager.phantom_entry_prevented/ { phantom++ }
    /position_manager.enter_blocked_phantom_cooldown/ { cooldown_block++ }
    /position_manager.entry_canceled\"/    { entry_canceled++ }
    /position_manager.entry_confirmed/      { confirmed++ }
    /position_manager.entry_unverifiable/   { unverifiable++ }
    /position_manager.orphan_adopted/       { orphan++ }
    /position_manager.settlement_orphan_redirect_counted/ { exp_redirect++ }
    /position_manager.completed_trades_bumped/ { counter_bump++ }
    /coordinator.live_entry_skipped_pm_refused/ { coord_skip++ }
    END {
      printf \"  order_placed                   : %5d\n\", placed
      printf \"  poll_order_early_bailout       : %5d  (BUG-022 Fix C)\n\", bailout
      printf \"  entry_canceled_on_timeout      : %5d  (BUG-022 Fix A)\n\", cancel_ok
      printf \"  entry_cancel_already_terminal  : %5d  (404 race, benign)\n\", cancel_404
      printf \"  entry_cancel_failed            : %5d  (non-404 cancel error)\n\", cancel_err
      printf \"  entry_filled_after_cancel      : %5d  (race won by Kalshi, recovered)\n\", filled_after_cancel
      printf \"  entry_poll_disagrees_exchange  : %5d  (poll said 0, exchange said >0)\n\", disagree
      printf \"  entry_canceled (Kalshi-canceled): %5d\n\", entry_canceled
      printf \"  entry_unverifiable             : %5d  (DESYNC)\n\", unverifiable
      printf \"  entry_confirmed                : %5d  (entry succeeded)\n\", confirmed
      printf \"  phantom_entry_prevented        : %5d  (entry rejected, FLAT)\n\", phantom
      printf \"  enter_blocked_phantom_cooldown : %5d  (BUG-022 follow-up b: per-ticker cooldown)\n\", cooldown_block
      printf \"  orphan_adopted                 : %5d  (BUG-022 should drive to ~0)\n\", orphan
      printf \"  settlement_orphan_redirect_cnt : %5d  (BUG-022 follow-up c: full-position 409 redirect)\n\", exp_redirect
      printf \"  completed_trades_bumped        : %5d  (BUG-022 follow-up c: orphan-recovery counter advance)\n\", counter_bump
      printf \"  coord_live_entry_skipped       : %5d  (coordinator pre-check refused entry)\n\", coord_skip
    }
'"
echo ""

echo "── Live trade outcomes by exit_reason (DB) ──"
ssh "${REMOTE}" "docker exec kbtc-db psql -U kalshi -d kbtc -t -c \"
  SELECT exit_reason, count(*) AS n,
         to_char(sum(pnl)::numeric, 'FM\$999990.00') AS total_pnl
    FROM trades WHERE trading_mode='live'
    GROUP BY exit_reason ORDER BY n DESC;
\""
echo ""

echo "── Live orphan/expiry exits by day (DB) ──"
ssh "${REMOTE}" "docker exec kbtc-db psql -U kalshi -d kbtc -t -c \"
  SELECT date_trunc('day', timestamp)::date AS day,
         count(*) FILTER (WHERE exit_reason='ORPHAN_SETTLED') AS phantom_orphans,
         count(*) FILTER (WHERE exit_reason='EXPIRY_409_SETTLED') AS expiry_409,
         count(*) AS total_trades,
         round(100.0 * count(*) FILTER (WHERE exit_reason='ORPHAN_SETTLED')
               / NULLIF(count(*),0), 1) AS phantom_pct
    FROM trades WHERE trading_mode='live'
    GROUP BY 1 ORDER BY 1 DESC LIMIT 14;
\""
echo ""

echo "── Health summary ──"
ssh "${REMOTE}" "curl -sf http://localhost:8000/api/status 2>/dev/null" \
  | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    live = d.get('live', {})
    print(f\"  live state               : {live.get('state','?')}\")
    print(f\"  live can_enter           : {live.get('can_enter')}\")
    print(f\"  live trade limit         : {live.get('completed_live_trades')}/{live.get('live_trade_limit')}\")
    print(f\"  live orphans             : {len(live.get('orphaned_positions', []))}\")
    print(f\"  live bankroll            : \${d.get('live_bankroll', 0):.2f}\")
    print(f\"  paper bankroll           : \${d.get('paper_bankroll', 0):.2f}\")
except Exception as e:
    print(f\"  status fetch failed: {e}\")
" 2>/dev/null
echo ""
echo "Tip: re-run this script after the next live entry to confirm phantom rate dropping."
