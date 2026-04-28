# Tier 1 (original) — ML foundations: execution-quality features, live backfill, mode-aware training

**Date**: 2026-04-28
**Status**: Shipped to production
**Related**:
- [`docs/findings/2026-04-28-tier0-live-vs-paper.md`](2026-04-28-tier0-live-vs-paper.md) — Tier 0 diagnosis
- [`docs/findings/2026-04-28-tier1-revised-completion.md`](2026-04-28-tier1-revised-completion.md) — data-quality cleanup that unblocked this
- [`docs/findings/2026-04-28-three-recommendations.md`](2026-04-28-three-recommendations.md) — yesterday's edge-filter work
- [`backend/migrations/008_execution_quality_features.sql`](../../backend/migrations/008_execution_quality_features.sql)

---

## TL;DR

Three pieces shipped, in this order:

1. **1b — Execution-quality features (high leverage).** Added 4 new features
   to `trade_features` and `extract_features()` that describe how well a trade
   is likely to *fill*, not just what the market state looks like:
   `minutes_to_contract_close`, `quoted_spread_at_entry_bps`,
   `book_thickness_at_offer`, `recent_trade_count_60s`. Capturing started
   immediately for both paper and live going forward. The next retrain in
   ~7 days will produce the first model that learns from them.

2. **1a — Live `trade_features` backfill (modest win).** Wrote
   `scripts/backfill_live_trade_features.py` that joins `trades` ↔
   `ob_snapshots` and reconstructs entry-time features for historical live
   trades. Lifted the labeled-live count from **7 → 16 rows** (9 new
   inserts, 12 unrecoverable). The "16 vs 24 clean live trades" gap is
   closed as far as the historical record allows.

3. **1c — `--mode {paper,live,both}` flag (plumbing only).** Trainer and
   promotion script now accept `--mode`. `--mode live` enforces a
   `MIN_LIVE_ROWS_FOR_LIVE_MODE = 50` minimum so an operator can't
   accidentally ship a model trained on a handful of rows. The
   `training_mode` is recorded in the artifact, the meta JSON, and the
   `ml.model_loaded` log line at bot startup. We don't *use* `--mode live`
   in production yet — we're nowhere near 50 clean live rows — but the
   infrastructure is ready.

Production deploy: `kbtc-bot` healthy, model loads cleanly, no errors.
Tests: **29 ML-related tests pass, full suite 369 pass.**

---

## Why this work happened in this order (and not the original Tier 1 order)

The original Tier 1 plan from 2026-04-27 was: backfill features → add
execution features → train a live-only model. Doing inventory before
implementing surfaced two facts that flipped the priority:

| Original assumption | Reality |
|---|---|
| 34 live trades to backfill from | 24 are flag-clean (10 are data-quality-flagged) |
| Reconstructable from `signal_log` ↔ `ob_snapshots` | `signal_log` is **mostly empty** for historical live trades — only 5 of 34 have a row in the prior 5 min |
| Should jump from 7 → 34 labeled live rows | At best **15-18** — about half the live trades have **zero `ob_snapshots`** in the prior 5 min (the bot wasn't continuously snapshotting that ticker) |
| Existing 7 live feature rows are usable | Only 3 are flag-clean (the other 4 are PRE_BUG027_WALLET / CORRUPTED_PNL) |

So the leverage rank-order changed:

- **1b execution-quality features** went from "important" to "the only thing
  that's likely to move the needle in the next 30 days" because:
  - The current 14 features describe market state but say nothing about
    execution quality.
  - The SHORT_SETTLEMENT_GUARD analysis on 2026-04-28 just proved that
    execution-window mechanics (specifically: time-to-close on shorts)
    were dominating live PnL.
  - Capturing starts immediately on both paper and live, so within ~7
    days we have 200+ rows with the new features.

- **1a backfill** went from "the foundation step" to "useful diagnostic
  data" — 16 labeled live rows is too small for training but enough to
  enable per-trade comparison of "what feature values did paper see vs
  live?" for the same conditions.

- **1c live-only model** stayed at the back: no point implementing the
  training pipeline until we have ≥50 labeled live rows, but the
  *plumbing* (the flag, the guard, the mode-tracking) is cheap to build
  and gets us ready for the day the threshold is met (estimated 4-6
  weeks once v2 features are live).

User explicitly approved this re-sequencing.

---

## 1b — Execution-quality features

### What was added

Migration `008_execution_quality_features.sql` adds 4 columns to
`trade_features`:

```sql
minutes_to_contract_close   NUMERIC(8,3)
quoted_spread_at_entry_bps  INTEGER
book_thickness_at_offer     NUMERIC(20,2)
recent_trade_count_60s      INTEGER
```

`extract_features()` (`backend/ml/feature_capture.py`) computes them at
trade entry from objects already available in the coordinator:

| Feature | Source | Compute |
|---|---|---|
| `minutes_to_contract_close` | `state.time_remaining_sec` | `time_remaining_sec / 60` |
| `quoted_spread_at_entry_bps` | `features.spread_cents`, `features.mid_price` | `(spread / mid) × 10000` |
| `book_thickness_at_offer` | `state.order_book.book_thickness_within(mid, ±5c)` | sum bids+asks within 5c of mid (direction-agnostic, computed before direction is finalized) |
| `recent_trade_count_60s` | latest candle's `tick_count` | direct passthrough |

`book_thickness_within` is a new helper on `OrderBookState` (`backend/data/manager.py`).

### Tests

11 new tests added to `backend/tests/test_feature_capture.py` plus 1
schema-presence test, all passing. The existing `test_train_serve_features.py`
contract still holds: every `ENTRY_FEATURES` name is a key produced by
`extract_features()`.

### Why a model trained pre-2026-04-28 keeps working

The current production model (`xgb_entry_v1.pkl`) was trained on the old
14 features. It iterates over `_artifact["features"]` at inference time
and extracts only those keys from `feature_dict.get(f, 0)`. The 4 new
features are simply ignored. Backwards compatibility is total. Once the
next retrain happens with enough new-feature rows, the candidate model
will include them — the existing precision-floor + regression-tolerance
promotion gate will reject it if performance degrades.

### Expected v2 timeline

- **Day 0 (today)**: Bot captures 4 new features on every paper + live trade.
- **Day 7-10**: ~150-300 paper rows have the new features populated.
- **Next Sunday cron retrain**: Trains on the mixed dataset (NULLs zero-filled
  for old rows). Promotion gate decides if it's an improvement.
- **Day 30**: ~600-1000 paper rows with new features. Feature importance
  becomes meaningful. We can read which of the 4 actually matters and
  prune the dead ones.

---

## 1a — Live `trade_features` backfill

### Script: `scripts/backfill_live_trade_features.py`

Idempotent (skips trades that already have a row). Dry-run mode. Joins
`trades` to `ob_snapshots` (within 60s before entry) and `kalshi_trades`
(60s window for `recent_trade_count_60s`). For each eligible trade,
reconstructs:

- ✓ `obi`, `bid_depth`, `ask_depth`, `spread_pct` (from snapshot aggregates)
- ✓ `book_thickness_at_offer`, `quoted_spread_at_entry_bps` (from snapshot ladders)
- ✓ `minutes_to_contract_close`, `time_remaining_sec` (from ticker + trade ts)
- ✓ `hour_of_day`, `day_of_week` (from trade ts)
- ✓ `recent_trade_count_60s` (from kalshi_trades)
- ✓ `pnl`, `label` (from trades — already-captured outcome data)
- ✗ `roc_3 / roc_5 / roc_10` (need 1m candle history — not stored)
- ✗ `atr_pct` (need spot history)
- ✗ `green_candles_3`, `candle_body_pct`, `volume_ratio` (need 1m candles)
- ✗ `tfi`, `taker_buy_vol`, `taker_sell_vol` (need historical_sync state)

The unrecoverable fields are inserted as NULL. `train_xgb.py` zero-fills
NULLs. The cost of this for the backfilled rows specifically is that the
model sees them as "ROC was zero, ATR was zero" — a known data-quality
caveat, but acceptable for 9 rows in a dataset that will grow well past
500 in the coming weeks.

### Scope (decided with user)

- `trading_mode = 'live'` only
- `data_quality_flag IS NULL` only (skip CATASTROPHIC_SHORT,
  PRE_BUG027_WALLET, EXIT_REASON_RECLASSED, CORRUPTED_PNL)
- Must have at least one `ob_snapshot` within 60s before entry

### Result

```
eligible live trades: 21
recoverable:           9
skipped:              12  (no_snapshot_within_60s)

trade_features (live, labeled):  7 → 16
```

Why only 9 of 21? `ob_snapshots` was not continuously populated during
the early live-trading period (trades 410, 420, 422, 436, 454, 481, 677,
691, 700, 746, 783, 862). `kalshi_trades` only goes back to 2026-04-16
which is after most of the early live trades — that's why
`recent_trade_count_60s` is 0 on every backfilled row. Both are honest
limits of the historical record.

---

## 1c — `--mode {paper,live,both}` training flag

### What changed

- `scripts/train_xgb.py::load_data` now takes a `mode` argument that
  filters by `trading_mode` before training. Defaults to `"both"`
  (preserves historical cron behavior).
- `scripts/train_xgb.py::train` now takes a `training_mode` kwarg and
  records it in the artifact dict and the meta JSON.
- `scripts/train_xgb.py::main` exposes `--mode` on the CLI.
- `scripts/retrain_promote.py::main` exposes `--mode` and passes it
  through. The promotion-decision logic now logs a `WARN` when the
  candidate's `training_mode` differs from the incumbent's (a quiet
  swap could slip past otherwise). The "candidate has fewer rows than
  incumbent" guard is bypassed when modes differ (their row counts
  aren't comparable).
- `MIN_LIVE_ROWS_FOR_LIVE_MODE = 50`. `--mode live` raises a `ValueError`
  with a fix-it message if the loaded dataset has fewer rows.
- `backend/ml/inference.py::load_model` now logs `training_mode` so an
  operator can see at startup which population the active model was
  trained on.

### Tests

6 new tests in `backend/tests/test_train_xgb_mode_flag.py`:

1. Default `mode='both'` returns all rows (cron compatibility).
2. `mode='paper'` filters correctly.
3. `mode='live'` raises with helpful message when below threshold.
4. `mode='live'` succeeds exactly at the threshold.
5. Invalid mode raises immediately.
6. `mode='paper'` / `mode='live'` requires a `trading_mode` column;
   `mode='both'` does not.

These exercise `load_data` only — not `train()` — to avoid pulling
xgboost/sklearn into the bot's runtime test path.

### When to actually use `--mode live`

Not yet. We have **16 labeled live rows** (after the backfill). To get
to 50 we need ~34 more clean live trades, which at the current live
trade-rate (a few per day at best) is a 4-6 week timeline.

When that day comes, the recommended workflow is:

1. Verify `SELECT COUNT(*) FROM trade_features WHERE trading_mode='live'
   AND label IS NOT NULL AND data_quality_flag IS NULL` is ≥ 50 (using
   the join to `trades`).
2. Train the live-only candidate alongside the both-mode incumbent:
   `python scripts/retrain_promote.py --mode live --dry-run` (will train
   but not promote).
3. Compare candidate-precision-on-live-tail vs incumbent-precision-on-live-tail
   on a separate held-out evaluation set (script does not exist yet —
   build when needed).
4. If live-only wins by a meaningful margin AND the population isn't
   suspect (no recent regime shift, no policy change), promote with
   `python scripts/retrain_promote.py --mode live`.

---

## Files changed

```
backend/migrations/008_execution_quality_features.sql        (NEW)
backend/data/manager.py                                       (book_thickness_within helper)
backend/ml/feature_capture.py                                 (4 new features in extract + save)
backend/ml/inference.py                                       (log training_mode)
backend/tests/test_feature_capture.py                         (11 new tests)
backend/tests/test_ml_inference_integration.py                (synthetic frame extended for v2 cols)
backend/tests/test_train_xgb_mode_flag.py                     (NEW, 6 tests)
scripts/backfill_live_trade_features.py                       (NEW)
scripts/init_db.sql                                           (4 new cols on fresh deploys)
scripts/train_xgb.py                                          (--mode flag, MIN_LIVE_ROWS, training_mode persistence)
scripts/retrain_promote.py                                    (--mode flag, mode-mismatch warn)
docs/findings/2026-04-28-tier1-original-ml-foundations.md     (NEW — this file)
```

---

## Validation in production

1. **Migration applied**: `\d trade_features` shows the 4 new columns.
2. **Bot loaded the (still-old) v1 model cleanly**: `ml.model_loaded`
   log shows `features=14, training_mode=unknown`. No
   `ml.model_load_failed` or `ml.gate_inference_error` events.
3. **Backfill applied**: `trade_features` count for `trading_mode='live'`
   went from 7 → 16.
4. **No new errors**: bot logs are clean, normal "skipped near expiry"
   activity, no exceptions.

## What to watch for the next 7 days

- **First paper trade row should have all 4 v2 features non-null.**
  Verify with: `SELECT * FROM trade_features WHERE timestamp > <today>
  AND trading_mode='paper' ORDER BY timestamp DESC LIMIT 1\gx`
- **Sunday's retrain cron should still pass the promotion gate.**
  Watch the `.promotion_log.json` — if it fails because the new feature
  columns are mostly NULL on existing rows and the model degrades, that's
  fine; the existing v1 stays in production.
- **Per-day count of new feature populations** (sanity check that capture
  is wired right): `SELECT DATE(timestamp), COUNT(*) FILTER (WHERE
  minutes_to_contract_close IS NOT NULL) FROM trade_features WHERE
  timestamp > NOW() - INTERVAL '7 days' GROUP BY 1 ORDER BY 1;`

---

## What's NOT done (by design)

- **Trained a v2 model.** Need the new features to populate ~200+ paper
  rows first. Sunday's cron will produce the first attempt; manual
  retrains in the meantime are wasted compute.
- **Trained a `--mode live` model.** Only 16 labeled live rows; threshold
  is 50.
- **Wired training_mode into ml.gate_inference_error logs.** The artifact
  carries it; we just don't log it on every gate call (would be noisy).
  Visible at startup is enough.
- **Backfilled the 12 trades we couldn't reconstruct.** No `ob_snapshots`
  in the prior 5 min → no honest reconstruction. They stay missing.
