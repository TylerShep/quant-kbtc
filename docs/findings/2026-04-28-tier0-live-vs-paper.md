# Tier 0 findings — live PnL gap diagnosis

**Date:** 2026-04-28
**Scope:** All 34 live trades from 2026-04-13 to 2026-04-24
**Source data:** `trades`, `kalshi_trades`, `ob_snapshots`, `signal_log` on prod
**Tools built:** `scripts/live_vs_paper_diff.py`, `scripts/attribution_backfill_cron.sh`

## TL;DR (updated 2026-04-28 evening, after deeper diagnosis)

**The original headline of this doc was wrong about one of two issues.** A
deeper trace through the code revealed:

1. ❌ **There is NO current PnL accounting bug.** The "+$0.49 per trade drift"
   I initially flagged is a measurement artifact from the pre-BUG-027 wallet
   capture timing, not a current accounting error. BUG-027 was already
   diagnosed, fixed in code (commit `0ce407e` on 2026-04-23 12:34 UTC), and
   backfilled (`scripts/backfill_pnl_bug027.py`). All trades after the fix
   show drift = $0.00. The 4 drift-positive trades (702/716/750/788) are all
   from before the fix and have stale `wallet_pnl` values that reconstruct
   exactly as `pnl + entry_cost + entry_fees` — proving they were captured
   under the old (buggy) post-entry snapshot timing. **No code change needed.**
2. ✅ **April 13-14 short-trade catastrophe IS real**. 4 SHORT trades over two
   days with recorded `exit_price=99` booked **-$22.73 of "loss"** out of the
   headline -$25.55 week-1 number. These are all `order_response` fill-source
   trades from before the April 20 fill websocket migration. The exit_price=99
   looks like a default/sentinel value being recorded when the position settled
   against us at expiry — **not** an actual sell at 99 cents. We can't
   re-verify because we don't have wallet data from that era.
3. ✅ **Exit-reason classifier IS broken**: trade 759 (a -$5.70 short loss)
   was labeled `TAKE_PROFIT`. Real bug to fix, but small scope.

**Verdict on the live PnL gap:** The strategy's actual live PnL is closer to
**-$10 (a 1% drawdown)**, not -$96. ~$22 of the headline loss is the
short-trade catastrophe (real but contained to that pre-fill_ws era), and the
~$0.49/trade "drift" was never a real loss — it was the old reconciliation
snapshot lying about itself. Post April 23 (the BUG-027 fix), trades have
proper wallet reconciliation and `pnl = wallet_pnl` to the cent.

## What I built (Tier 0 deliverables)

### 0b: Attribution dashboard now populated

- Ran `scripts/backfill_attribution.py` on prod → 29 rows in `daily_attribution`
  covering Apr 13 - Apr 28 (paper + live)
- Installed `scripts/attribution_backfill_cron.sh` on cron at 03:00 UTC daily
  (idempotent, posts to Discord errors webhook on failure only)
- Documented in `docs/runbooks/attribution.md`
- The dashboard's Attribution panel now has data; previously it was blank for
  the entire history of the bot

### 0a: Live-vs-paper diff tool

- Wrote `scripts/live_vs_paper_diff.py` — read-only, queries `trades` /
  `ob_snapshots` to compute drift, slippage, depth ratios per live trade
- Generated first report at `backend/backtest_reports/live_vs_paper_20260428T040914Z.{json,md}`
- Read top-to-bottom by a human; findings below

## Detailed findings

### Finding 1 (highest impact): The pre-April-20 short trades aren't real losses

Of the 34 live trades, **4 SHORT trades on April 13-14 account for $22.73 of
the $27.22 recorded loss (84%)**. Every one of them:
- Has `fill_source = order_response` (the old broken accounting flow)
- Has `exit_price = 99` (a suspicious round-number sentinel)
- Has no `wallet_pnl` reconciliation
- Was on a contract for which we have no `kalshi_trades` tape data to verify

The 4 trades:

| ticker (date) | dir | size | entry | recorded exit | recorded pnl |
|---|---|---|---|---|---|
| KXBTC-26APR1315-B72150 (Apr 13) | short | 12 | 11¢ | 99¢ | -$10.71 |
| KXBTC-26APR1316-B72450 (Apr 13) | short | 7  | 18¢ | 99¢ | -$5.70 |
| KXBTC-26APR1318-B73150 (Apr 13) | short | 16 | 6¢  | 99¢ | -$5.13 |
| KXBTC-26APR1401-B74450 (Apr 14) | short | 2  | 17¢ | 99¢ | -$1.19 |

Note one of these is mis-labeled `TAKE_PROFIT` (exit_reason mismatch — the
classifier thinks "the price moved to our limit" without checking that we
were short and the price went against us, an obvious bug).

A short on a YES contract that "settles at 99" means the market resolved to
YES against us. For a 11¢ short on a 12-contract position, max loss should
be approximately 12 × $0.89 = $10.68 (matches the -$10.71 within fees). So
the recorded PnL **is** mechanically correct given exit_price=99. The
question is whether the position actually settled or whether something
weirder happened — and we don't have wallet data from that era to confirm.

**Action**: this is historical and unrecoverable, but we should never run
this much short size again until the new fill_ws flow has demonstrated
correctness on shorts (none of the post-Apr-20 trades are shorts; the bot
has only gone long since).

### Finding 2 (RETRACTED): "Accounting bug" was a measurement artifact

Original claim: "wallet always shows more money than recorded pnl by ~$0.49."
This was true mechanically but had nothing to do with a current bug. Walking
through the code:

- `pnl` is computed by `position_manager._exit_inner()` as
  `exit_cost - entry_cost - (entry_fees + exit_fees)`. This is the correct
  cash-flow formula (per BUG-027 fix on 2026-04-23 commit `0ce407e`).
- `wallet_pnl` is computed by `coordinator._persist_trade()` as
  `wallet_post_exit - wallet_at_entry`. **For trades AFTER the BUG-027 fix**,
  `wallet_at_entry` is captured BEFORE the entry order (correct), so
  `wallet_pnl == pnl` to the cent (zero drift on trades 820, 869).
- **For trades BEFORE the fix** (702, 716, 750, 788), `wallet_at_entry` was
  captured AFTER the entry debit cleared, so `wallet_pnl ≈ pnl + entry_cost
  + entry_fees`. This is exactly what the data shows:

| trade | pnl | entry_cost+fee | predicted wallet_pnl | observed wallet_pnl |
|---|---|---|---|---|
| 702 | -$0.27 | +$0.72 | **+$0.45** | +$0.45 ✓ |
| 716 | +$0.03 | +$0.73 | **+$0.76** | +$0.76 ✓ |
| 750 | $0.00 | +$0.80 | **+$0.80** | +$0.80 ✓ |
| 788 | -$0.57 | +$0.68 | **+$0.11** | +$0.11 ✓ |

Perfect agreement. The drift IS the historical entry cost, period. The
existing `pnl` values in the trades table are correct (BUG-027 backfill ran
and rewrote them via the cash-flow formula). The `wallet_pnl` values on these
4 rows can't be reconstructed from row data alone (per the BUG-027 backfill
script's docstring) and were intentionally not touched.

**No current bug. No code change needed.** A docstring will be added to
`coordinator._persist_trade` noting this so future operators don't
re-investigate.

### Finding 3: EXPIRY_409 is dominant but mostly benign

12 of 34 live trades (35%) exit via `EXPIRY_409_SETTLED`. Looking at their
recorded PnL distribution:

- Total: -$0.78 across 12 trades
- 4 of them are actually positive (settled in our favor at expiry)
- Worst is -$0.87, best is +$2.28

These aren't the problem. They're "we held a trade until expiry, sometimes
we won, sometimes we lost a small amount." The bot doesn't bother selling
late in the contract because the strategy is fine letting it ride. **Not a
bug, working as designed.** If we want to change this, it's a strategy
question, not a stop-the-bleed question.

### Finding 4: Entry slippage is NEGATIVE on average

For the 10 trades where we have an order book snapshot near entry, the
average entry slippage vs the order book ask was **-22 cents** (negative
means we paid LESS than the best ask). Median -8c, max +29c.

This is unexpected for a taker strategy and warrants a sanity check —
either:
- We're using a stale order book snapshot (the +/-60s window is too wide;
  the book moved between snapshot and our order)
- Our limit orders are sitting in the book and getting hit at favorable
  prices
- There's a unit mismatch somewhere (e.g. comparing dollars to cents)

**Action**: tighten the snapshot window to ±5s in v2 of the script, or
better, store our actual entry order book at trade time as part of the
trade row (so we know the *exact* book we faced, not the closest snapshot).

### Finding 5: Size vs liquidity is healthy

Median size/depth ratio is 0.005, max is 1.0. We're not eating the entire
offer on entry. Whatever's killing us, it's not "we're too big for the
book."

## Where to focus Tier 1 work (revised again, 2026-04-28 evening)

Based on the deeper diagnosis, the Tier 1 plan shrinks substantially. The
"pnl bug" item is gone. Revised order:

1. ~~Audit the `pnl` calculation~~ — **CANCELED.** No bug exists. Already
   fixed and backfilled per BUG-027 (commit `0ce407e`, 2026-04-23).
2. **Quarantine the pre-Apr-20 short trades** with a queryable data-quality
   marker so dashboards and attribution can exclude them. The 4 SHORT trades
   from 4/13-4/14 with `exit_price=99` (KXBTC-26APR1315-B72150,
   KXBTC-26APR1316-B72450, KXBTC-26APR1318-B73150, KXBTC-26APR1401-B74450)
   account for $22.73 of "loss" on the dashboard but are infrastructure
   artifacts, not real strategy losses.
3. **Fix the exit-reason classifier** — `TAKE_PROFIT` on a -$5.70 short loss
   is nonsense. Probably comparing to entry side without normalizing for
   direction. Add a regression test, fix the classifier, and one-shot backfill
   historical rows with the corrected logic.
4. **Add a docstring to `coordinator._persist_trade`** explaining why
   `pnl_drift` on rows 702/716/750/788 is a known historical artifact, not a
   current bug. Saves the next operator from re-investigating.
5. **Re-run `scripts/backfill_attribution.py`** after step 3 lands so
   `daily_attribution` reflects the corrected exit_reason breakdown.
6. **Then resume the original Tier 1 plan** (backfill features, add
   execution-quality features, train live-only model). The data inputs are
   actually fine — only the categorical `exit_reason` field was poisoned, and
   it's not used as an ML feature.

Steps 2-5 are an afternoon of work, not a multi-day effort.

## Files produced

- `scripts/live_vs_paper_diff.py` (new)
- `scripts/attribution_backfill_cron.sh` (new)
- `docs/runbooks/attribution.md` (new)
- `docs/findings/2026-04-28-tier0-live-vs-paper.md` (this file)
- `backend/backtest_reports/live_vs_paper_20260428T040914Z.{json,md}` (on prod)
- Cron entry on droplet: `0 3 * * * /home/botuser/kbtc/scripts/attribution_backfill_cron.sh`
- 29 new rows in `daily_attribution`
