# 2026-04-28 — Three recommendations from "Why is live quiet?"

Follow-up to the 24-hour live-trading silence diagnosis. Three actions were
recommended; all three are now resolved.

## TL;DR

| Action | Status | Outcome |
|---|---|---|
| 1. Document the live edge-filter stack | ✅ shipped | New runbook `docs/runbooks/live-edge-filters.md` covering every filter, its config, and an operator playbook |
| 2. Investigate `SHORT_SETTLEMENT_GUARD` | ✅ diagnosed + fixed | The guard was a symptom; the bot was entering shorts inside the close window where settlement-time gamma blew them up. New entry-side guard `SHORT_ENTRY_TOO_CLOSE_TO_EXPIRY` blocks shorts when `time_remaining_sec < 780`. Affects both lanes |
| 3. Investigate `EDGE_PRICE_CAP` | ✅ analyzed + widened | Cap had never fired in production. Paper attribution showed 26–30¢ trades were +$2,768 net at 62% WR. Raised `EDGE_LIVE_MAX_ENTRY_PRICE` from 25 → 30 |

Both code/config changes shipped to prod 2026-04-28 ~14:25 UTC. Bot redeployed
cleanly, no errors, env vars confirmed in container.

---

## Action 1: Live-filter documentation

The live trading lane runs the same strategy as paper and then layers ~10
additional filters on top. Until today, the only place to learn what they do
was to read the code (`backend/filters/edge_profile.py`,
`backend/filters/trend_guard.py`, `backend/filters/price_guard.py`,
`backend/coordinator.py`).

The new runbook (`docs/runbooks/live-edge-filters.md`) covers, for each filter:

* What it blocks
* Where it lives in the codebase
* What env var controls it
* Why it exists (which paper-attribution finding justified the default)
* Current 7-day fire rate (with SQL to re-run)
* When an operator would want to tune it
* SQL snippets for counterfactual analysis

**Bonus content:** the runbook also includes the price-cap counterfactual
analysis that justified action #3, and the short-window data table that
justified action #2. So a future operator looking at "should I change this?"
can find the evidence in the same file.

## Action 2: `SHORT_SETTLEMENT_GUARD` was a symptom

### Diagnosis

In the 14 days leading up to 2026-04-28, `SHORT_SETTLEMENT_GUARD` fired 16 times
on paper trades, with a **0% win rate** and a total loss of **-$3,147**. The
guard itself was working correctly — the bug was upstream.

Bucketing the 21d of paper short trades by minutes-to-close at entry made the
pattern stark:

| min-to-close at entry | n shorts | avg PnL | total PnL | WR % | guard exits |
|---|---|---|---|---|---|
| 0–3 min | 9 | -$40 | -$364 | 56% | 3 |
| **4–5 min** | **14** | **-$196** | **-$2,747** | **0%** | **13** |
| 6–8 min | 1 | -$218 | -$218 | 0% | 0 |
| 9–12 min | 3 | -$1,097 | -$3,290 | 0% | 0 |
| **13–15 min** | **204** | **+$5** | **+$1,010** | **59%** | 0 |

Shorts entered with ≥13 min to close were 59% WR / +$1k net.
Shorts entered with <13 min were catastrophic: -$6,619 across 27 trades.

Concrete example of the failure mode (paper, 2026-04-28 13:49–13:58 UTC):

1. 13:49:48 — enter SHORT 2624 contracts at 26¢ (~10 min to contract close)
2. 13:56:10 — `SHORT_SETTLEMENT_GUARD` fires (price now 39¢, 229s remaining). Loss: -$420
3. 13:56:18 — bot immediately enters a NEW SHORT 1734 contracts at 39¢ (~3:42 to close)
4. 13:58:10 — guard fires AGAIN (price now 59.5¢, 109s remaining). Loss: -$413

The bot doubled its loss in 8 minutes by entering, getting blown up, and
re-entering on a fresh strike of the same expiring contract.

### Fix

New guard in `backend/filters/price_guard.py`:

* Direction = short AND `time_remaining_sec < short_min_seconds_to_expiry` →
  reject with `SHORT_ENTRY_TOO_CLOSE_TO_EXPIRY_*s<*s`
* Default `short_min_seconds_to_expiry = 780` (13 min), env-configurable via
  `SHORT_MIN_SECONDS_TO_EXPIRY`
* Affects **both paper and live** — paper has been the bleeding source; live
  was already protected by `EDGE_SHORT_BLOCKED`

Also removed a misguided clause in the same file that *widened* the short
max-price bound by 5c when `time_remaining_sec < 300`. That was making the
filter MORE permissive exactly inside the gamma blow-up window. The new
hard-block above renders that loosening moot, and there was no empirical
reason for it in the first place.

Tests added in `backend/tests/test_price_guard.py`:

* `test_short_blocked_inside_expiry_window` — short at 779s rejected
* `test_short_allowed_at_or_above_expiry_window` — short at 780s and 900s allowed
* `test_long_unaffected_by_short_expiry_guard` — long at 400s passes through
* `test_short_expiry_guard_runs_before_other_short_bounds` — ordering check
* `test_short_expiry_guard_does_not_fire_when_remaining_unknown` — `None` case

All 8 price_guard tests pass. (Two pre-existing test failures in `test_obi.py`
and `test_orphan_fixes.py` were verified to be unrelated to this change by
stashing it and re-running.)

### Validation plan

After 7 days of paper trading with the new guard, expect:

1. Zero or near-zero `SHORT_SETTLEMENT_GUARD` exits in `paper.exit` log events
2. `signal_log.skip_reason` shows `SHORT_ENTRY_TOO_CLOSE_TO_EXPIRY_*` in the
   same time windows where the guard used to fire
3. Net short PnL on paper improves by approximately the historical loss rate
   (~$3.1k / 14d for guard-exits alone, plus another ~$3.5k / 14d from the
   pre-guard losses in the 6–12 min bucket)

If the historical numbers held, this should swing paper-side shorts from
losing ~$6.6k / 14d to roughly break-even. Anything closer to break-even
unlocks the next conversation: should we re-enable shorts in the live lane?

## Action 3: `EDGE_PRICE_CAP` widened from 25¢ to 30¢

### Investigation

`EDGE_PRICE_CAP` had **never fired in production** since the live profile was
enabled. (Skip-reason count for `EDGE_PRICE_CAP%`: 0 across all of `signal_log`.)
That meant the cap was either redundant or genuinely sitting just below the
trade flow. To find out, ran a counterfactual against 21 days of paper LONG
trades (filtered to exclude `data_quality_flag` artifacts):

| Bucket | n | avg PnL | total PnL | WR % |
|---|---|---|---|---|
| ≤25c | 212 | +$235 | +$49,856 | 58% |
| **26–30c** | **34** | **+$81** | **+$2,768** | **62%** |
| 31–35c | 25 | +$44 | +$1,110 | 48% |
| 36–40c | 29 | -$56 | -$1,612 | **17%** |
| 41–50c | 12 | +$18 | +$219 | 50% |
| >50c | 15 | -$73 | -$1,091 | **13%** |

The 26–30¢ bucket was 34 trades, 62% WR (slightly *better* than ≤25¢), and
+$2,768 net. The 31–35¢ bucket was marginal but still positive. Above 35¢
becomes a hard cliff — 17% WR in the 36–40¢ bucket.

Verdict: raising the cap to 30¢ is well-supported. Going to 35¢ was offered
but rejected as too aggressive given the cliff at 36¢.

### Change

* `EDGE_LIVE_MAX_ENTRY_PRICE` raised from `25.0` → `30.0` on prod `.env`
* No code change needed — this is a pure config knob
* `.env.example` updated to match (so new operators see the post-2026-04-28 default)

### What to watch

* `signal_log.skip_reason` may start showing `EDGE_PRICE_CAP_*` rejections in
  the 31¢+ range — that's the cap doing its job at the new threshold
* New live trades may begin appearing at 26–30¢ entry prices — that's the
  intended unlock
* If live PnL on the 26–30¢ trades looks materially worse than the paper
  attribution suggested, that's a sign of a paper/live execution gap and
  worth investigating before raising further

---

## Files changed

```
backend/config/settings.py              + short_min_seconds_to_expiry config
backend/filters/price_guard.py          + SHORT_ENTRY_TOO_CLOSE_TO_EXPIRY guard
                                        - removed misguided "widen on <300s" clause
backend/tests/test_price_guard.py       + 5 new tests, updated 2 existing time_remaining_sec
                                          values from 600 to 900 to be safely above the
                                          new 780s threshold
docs/runbooks/live-edge-filters.md      NEW — full operator runbook for the entire
                                              live filter stack
docs/findings/2026-04-28-three-          NEW — this file
recommendations.md
.env.example                            + SHORT_MIN_SECONDS_TO_EXPIRY=780,
                                          updated EDGE_LIVE_MAX_ENTRY_PRICE=30.0
```

Prod `.env` (not in source control):

```
EDGE_LIVE_MAX_ENTRY_PRICE=25.0  → 30.0
SHORT_MIN_SECONDS_TO_EXPIRY  → 780  (new line)
```

## Next session

1. Check back in 24–48 h on the SHORT_SETTLEMENT_GUARD count in paper. If it's
   near zero and net short PnL has visibly improved, the fix worked.
2. Watch for any live trades at 26–30¢ entry. Compare to paper performance.
3. If both look good after a week, two follow-up moves to consider:
   * Lower `SHORT_MIN_SECONDS_TO_EXPIRY` toward 600 (10 min) gradually if the
     11–12 min cohort starts to look recoverable
   * Re-evaluate `EDGE_SHORT_BLOCKED` — with the entry-side guard preventing
     the worst short scenario, the live-lane block on shorts is more aggressive
     than necessary. A shadow-only short re-enable on live could be the next test
