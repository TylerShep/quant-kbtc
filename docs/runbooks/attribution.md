# Daily attribution runbook

`daily_attribution` is the row-per-(date, trading_mode) table that decomposes
each day's PnL into regime, signal, session, and execution components. It's
the foundation for the weekly digest, the dashboard's attribution panel, and
any "is this strategy still working?" question.

| | |
|---|---|
| Population (primary) | Bot-internal `_schedule_daily_attribution` runs at 00:00 UTC daily for yesterday's trades, then posts a Discord embed to the attribution channel |
| Population (safety net) | `scripts/attribution_backfill_cron.sh` runs at 03:00 UTC nightly. Idempotent — fills any (date, mode) pair with trades but no attribution row yet. Silent on success, posts to Discord errors webhook on failure. |
| Source data | `trades` table (one row per closed position) |
| Consumer (operator) | Dashboard's "Attribution" panel + weekly Discord digest (Monday 09:00 UTC) |

## Why we need both the in-bot scheduler AND the cron

The bot's `_schedule_daily_attribution` is the primary path. The cron exists because:

1. **Bot restarts skip days.** If the bot was down at 00:00 UTC on a given day,
   that day's attribution is never computed. The cron catches this on the next
   nightly pass.
2. **Retroactive backfills.** If new fields are added to the attribution
   computation (e.g. a new regime breakdown), running the backfill script
   recomputes historical days with the new logic. `ON CONFLICT DO UPDATE`
   makes this safe.
3. **Manual recovery.** When trade data is corrected (e.g. a `pnl` value gets
   recalculated), the next nightly cron picks up the fix automatically.

## What's in the `attribution` JSONB column

Each row's `attribution` jsonb has six keys:

| Key | What it contains |
|---|---|
| `total_trades` | Mirror of the column for convenience |
| `total_pnl_dollars` | Mirror of `total_pnl` |
| `regime_attribution` | PnL split by ATR regime at entry (LOW / MEDIUM / HIGH) |
| `signal_attribution` | PnL split by conviction (HIGH / NORMAL / LOW) and signal driver (OBI / ROC / OBI+ROC / ...) |
| `session_attribution` | PnL split by Asia / Europe / US session |
| `execution_attribution` | `actual_pnl`, `theoretical_pnl`, `execution_drag` (currently fees-only — slippage capture is Tier 1 work) |
| `exit_reason_breakdown` | Per `exit_reason` (TAKE_PROFIT / STOP_LOSS / EXPIRY_409_SETTLED / ...): count, % of all, avg pnl_pct, total pnl |

The full computation lives in `backend/backtesting/attribution.py::run_attribution()`.

## Operator actions

### Reading the dashboard

The dashboard's Attribution panel reads directly from this table. If it shows
"no data" for today, that's expected — today's trades aren't attributed until
00:00 UTC tomorrow (then re-checked at 03:00 UTC by the cron).

### Manually backfilling

For ad-hoc rebuilds (e.g. after an attribution module change):

```bash
ssh "$KBTC_DEPLOY_HOST"

# Dry-run first (compute but don't write)
docker run --rm \
  --user 1000:1000 \
  --network kbtc_kbtc-net \
  -v /home/botuser/kbtc/backend:/app \
  -v /home/botuser/kbtc/scripts:/scripts \
  -w /app \
  -e PYTHONPATH=/app -e HOME=/tmp \
  -e DATABASE_URL='postgresql://kalshi:kalshi_secret@db:5432/kbtc' \
  --entrypoint python kbtc-bot:latest \
  /scripts/backfill_attribution.py --dry-run

# Real run (idempotent, safe to repeat)
/home/botuser/kbtc/scripts/attribution_backfill_cron.sh
```

To restrict to one mode:

```bash
docker run --rm ...same args... \
  /scripts/backfill_attribution.py --mode live
```

### Interpreting an empty day

If `daily_attribution` shows `total_trades=0, total_pnl=0` for a day:

- **Live mode, 0 trades**: bot was paused, or all signals were filtered out by
  the gates (ATR / edge profile / ML gate). Cross-check `signal_log` to see
  what the bot saw.
- **Paper mode, 0 trades**: very unusual. Check that the paper trader was
  running (it's always-on by design). If not, the bot may be in an error state.
- **Either mode, missing row entirely**: the cron didn't run or failed. Check
  `/home/botuser/kbtc/logs/attribution_backfill.log` and the errors-channel.

### Verifying the cron is healthy

```bash
ssh "$KBTC_DEPLOY_HOST"

# Last cron run
tail -20 /home/botuser/kbtc/logs/attribution_backfill.log

# Most recent attribution row (should be from yesterday or earlier today)
docker exec kbtc-db psql -U kalshi -d kbtc -c \
  "SELECT date, trading_mode, total_trades, total_pnl FROM daily_attribution
   ORDER BY date DESC, trading_mode LIMIT 4;"

# Crontab shows the entry
crontab -l | grep attribution
```

## Known limitations

- **`execution_drag` is fees-only.** Today the field captures Kalshi fees but
  not slippage (the gap between the price the strategy assumed and the price
  the bot actually traded at). Adding true slippage attribution requires
  joining `trades` against an order-book snapshot at entry/exit time — that's
  Tier 1 work and is the subject of `scripts/live_vs_paper_diff.py`.
- **Per-trade granularity is lost.** This table is by-day-by-mode aggregated.
  For per-trade analysis, query `trades` directly. The aggregation is
  intentional: it's the unit the dashboard panel and weekly digest care about.
- **No `trading_mode='live' AND total_trades=0` row collapse.** The cron and
  in-bot scheduler both happily insert empty rows for days with no live
  trades. This shows up as gaps in the dashboard. Acceptable for now.

## Related files

- `backend/backtesting/attribution.py` — core attribution math
- `scripts/backfill_attribution.py` — one-shot backfill (manual or cron)
- `scripts/attribution_backfill_cron.sh` — cron wrapper around the above
- `backend/coordinator.py::_schedule_daily_attribution` — primary in-bot path
- `backend/notifications.py::DiscordNotifier.daily_attribution_report` — Discord embed
- `backend/migrations/005_fix_daily_attribution_pk.sql` — composite PK on (date, trading_mode)
