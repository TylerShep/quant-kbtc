# Live-lane edge filters runbook

The live trading lane runs the **same** strategy as paper trading and then applies
a stack of additional filters that only affect live orders. Paper trading is
deliberately unfiltered so it keeps generating clean training data.

This runbook is the source of truth for what each live filter does, what controls
it, and when an operator might want to tune it. **If you're staring at a quiet
live lane and a chatty paper lane, this is the page you want.**

The filters are evaluated in this order. The first one that rejects a signal
short-circuits the rest:

1. `ATR_REGIME_HIGH` — always-on regime gate
2. `SPREAD_WIDE_DOWNGRADE` — spread microstructure gate (downgrades, doesn't always block)
3. `SHORT_BLOCKED_UPTREND_*` — trend-aware short guard (paper + live)
4. `ML_GATE_REJECTED_*` — ML inference gate (currently shadow-only on paper)
5. `EDGE_SHORT_BLOCKED` — live-lane: block all shorts
6. `EDGE_LOW_CONVICTION_BLOCKED` — live-lane: block low-conviction
7. `EDGE_DRIVER_BLOCKED_*` — live-lane: block specific signal drivers
8. `EDGE_HOUR_BLOCKED_*UTC` — live-lane: block specific hours
9. `EDGE_PRICE_CAP_*c>*c` — live-lane: block expensive entries (price-cap, with OBI+ROC agreement exemption)
10. Price guard `EXPIRY_TOO_CLOSE` (`<180s`, both lanes) / `SHORT_ENTRY_TOO_CLOSE_TO_EXPIRY_*` (`<780s`, both lanes, **shorts only**) / `YES_PRICE_TOO_LOW` / `NO_PRICE_TOO_HIGH` / etc.
11. `SHORT_SETTLEMENT_GUARD` — exit-side, not entry: panics out of underwater shorts in the last 5 min of a contract

| | |
|---|---|
| Source of truth | `backend/filters/edge_profile.py` (live-only filters), `backend/filters/trend_guard.py` (short trend guard), `backend/filters/price_guard.py` (always-on price/expiry bounds), `backend/coordinator.py` lines ~370 (settlement guard) and ~1153 (edge-profile wiring) |
| Config dataclass | `EdgeProfileConfig` in `backend/config/settings.py` |
| Where rejections appear | `signal_log.skip_reason`, plus structured log event `coordinator.edge_profile_rejected` / `coordinator.short_settlement_guard` / `coordinator.ml_gate_rejected` / `trend_guard.short_blocked` |

---

## Why these filters exist (the empirical story)

The `EdgeProfileConfig` defaults were derived from a 7-day paper-trading attribution
study on 2026-04-13 → 2026-04-19 (245 clean trades, post bankroll-sizing fix).
The findings:

| Bucket | Result |
|---|---|
| Long side | t = 3.31, +$33/trade over 148 trades → **keep** |
| Short side | t = -2.17, -$15/trade over 97 trades → **block in live** |
| Entry price ≤ 25¢ | 59% WR vs 48% baseline → **price-cap at 25c** |
| OBI+ROC agreement | 89% WR (8/9, p≈0.020) → **exempt from price cap** |
| OBI/TIGHT driver | -$329 over 10 trades → **block** |
| ROC/TIGHT driver | 1d -$618 / 7d +$61, only thanks to a single windfall → **block** |
| Asia overnight (00–07 UTC) | +$1.83/trade, near zero edge → **block hours** |

A 21-day re-look (2026-04-07 → 2026-04-28, on the cleaned dataset with
`data_quality_flag` filtered out) confirms the long-side edge is still robust and
the short-side edge is still negative.

---

## Filter-by-filter reference

### `ATR_REGIME_HIGH`

| | |
|---|---|
| What it blocks | All entries (both lanes) when ATR % is in HIGH regime |
| Source | `backend/filters/regime.py` |
| Config | `ATRConfig` in `settings.py` (`ATR_LOW_THRESHOLD`, `ATR_HIGH_THRESHOLD`) |
| Why | Strategy was sized + tuned on LOW/MEDIUM regimes only. HIGH regime is fast-moving and our exit logic isn't fast enough |
| Rate (last 7d) | ~2,300 ticks |
| Tune by | Adjusting the ATR percentile thresholds, **never** by allowing trades in HIGH directly |

### `SPREAD_WIDE_DOWNGRADE`

| | |
|---|---|
| What it does | Downgrades conviction by one notch (HIGH → NORMAL → LOW), **does not block** |
| Source | `backend/strategies/spread_div.py`, applied in `coordinator.py` |
| Config | `SpreadDivConfig` (`SD_PCT_THRESHOLD`, `SD_WIDE_DOWNGRADE`) |
| Why | Wide spreads = thin book = our pre-trade fill model overestimates execution quality. Better to size down than to walk away entirely (we still want to learn from these signals in paper) |
| Rate (last 7d) | ~190 ticks |

### `SHORT_BLOCKED_UPTREND_*`

| | |
|---|---|
| What it blocks | Short entries when recent close-to-close rise exceeds a configurable threshold. Affects **both lanes** |
| Source | `backend/filters/trend_guard.py` |
| Config | `RiskConfig` in `settings.py`: `SHORT_TREND_LOOKBACK_CANDLES` (default 4), `SHORT_TREND_SOFTEN_RISE_PCT` (0.20%, downgrades conviction), `SHORT_TREND_BLOCK_RISE_PCT` (0.35%, blocks entry) |
| Why | Persistent short bias from the OBI signal in trending-up markets bleeds money. The guard reduces or blocks shorts when BTC is in an obvious uptrend |
| Rate (last 7d) | ~900 (suffix is the actual rise % observed) |

### `ML_GATE_REJECTED_p0.XX`

| | |
|---|---|
| What it blocks | Entries where the ML inference model predicts P(win) < `ML_MIN_P_WIN` |
| Source | `backend/ml/inference.py`, called from `coordinator.py` |
| Config | `MLConfig` in `settings.py`: `ML_GATE_ENABLED`, `ML_GATE_PAPER`, `ML_GATE_LIVE`, `ML_MIN_P_WIN` |
| Currently | Shadow mode on paper only. Live gate is OFF. Fail-open: missing model = trade allowed |
| Why | Add an ML overlay on top of rule-based signals to reject low-EV setups |
| Rate (last 7d) | ~95 paper rejections |
| Tune by | Adjust `ML_MIN_P_WIN` (default 0). Do NOT enable on live until you've evaluated several weeks of shadow performance — see `docs/runbooks/ml-retraining.md` |

### `EDGE_SHORT_BLOCKED`

| | |
|---|---|
| What it blocks | All short entries on the live lane |
| Source | `backend/filters/edge_profile.py::evaluate` |
| Config | `EdgeProfileConfig.long_only` env: `EDGE_LIVE_LONG_ONLY` (default `true`) |
| Why | Paper attribution study showed -$15/trade short edge with t = -2.17. Live blocks this entirely until shorts are fixed |
| Rate (last 7d) | 3,515 — by far the most-fired filter |
| Operator action | **Don't unblock until** the short-side losses in paper are addressed. As of 2026-04-28 the paper-side shorts are losing ~$3k/week, dominated by `SHORT_SETTLEMENT_GUARD` exits in the last few minutes of contracts (see SHORT_SETTLEMENT_GUARD section below) |

### `EDGE_LOW_CONVICTION_BLOCKED`

| | |
|---|---|
| What it blocks | All LOW-conviction entries on live |
| Source | `backend/filters/edge_profile.py::evaluate` |
| Config | `EdgeProfileConfig.block_low_conviction` env: `EDGE_LIVE_BLOCK_LOW_CONVICTION` (default `true`) |
| Why | LOW-conviction setups (e.g. neutral OBI + weak ROC) are noise-bait — paper data shows they're break-even at best after fees |
| Rate (last 7d) | 0 (most LOW-conviction signals get caught earlier by the trend guard or ATR regime) |

### `EDGE_DRIVER_BLOCKED_*`

| | |
|---|---|
| What it blocks | Live entries from any signal driver not in the allow-list |
| Source | `backend/filters/edge_profile.py::evaluate` |
| Config | `EdgeProfileConfig.allowed_drivers` env: `EDGE_LIVE_ALLOWED_DRIVERS` (default `OBI,OBI+ROC,ROC`) |
| Why | OBI/TIGHT and ROC/TIGHT (TIGHT-spread variants) underperformed in paper. ROC/TIGHT was removed 2026-04-21 after a 9-day counterfactual |
| Rate (last 7d) | ~73 (mostly `OBI/TIGHT` and `ROC/TIGHT`) |
| Operator action | Re-add a driver only after a walk-forward backtest shows it's net-positive |

### `EDGE_HOUR_BLOCKED_*UTC`

| | |
|---|---|
| What it blocks | Live entries during specific UTC hours |
| Source | `backend/filters/edge_profile.py::evaluate` |
| Config | `EdgeProfileConfig.blocked_hours_utc` env: `EDGE_LIVE_BLOCKED_HOURS_UTC` (default `0,1,2,3,4,5,6,7`) |
| Why | Asia overnight (00–07 UTC) showed near-zero edge on the long side and was a reliable source of paper losses on shorts. We pause live during those hours and let the bot collect data |
| Rate (last 7d) | ~180 |
| Operator action | Tighten or loosen by hour after re-running attribution. Don't change on the basis of a single rough night |

### `EDGE_PRICE_CAP_*c>*c`

| | |
|---|---|
| What it blocks | Live entries above the price cap (default 25¢), unless OBI+ROC agreement |
| Source | `backend/filters/edge_profile.py::evaluate` (post-`_get_entry_price` re-check at coordinator line ~1189) |
| Config | `EdgeProfileConfig.max_entry_price` env: `EDGE_LIVE_MAX_ENTRY_PRICE` (default `25.0`); `EdgeProfileConfig.agreement_overrides_price_cap` env: `EDGE_LIVE_AGREEMENT_OVERRIDES_PRICE_CAP` (default `true`) |
| Why | Sub-25¢ longs had 59% WR vs 48% baseline. Expensive entries pay too much for the 1.05× max payout |
| Rate (last 7d) | **0** — has never fired since the live profile was enabled. See "Counterfactual: would widening the cap help?" below |

### Price guard `EXPIRY_TOO_CLOSE` / `YES_PRICE_TOO_LOW` / `NO_PRICE_TOO_HIGH`

| | |
|---|---|
| What it blocks | Always-on entry price guards on both lanes |
| Source | `backend/filters/price_guard.py` |
| Config | `RiskConfig`: `LONG_MAX_ENTRY_PRICE` (60), `SHORT_MIN_ENTRY_PRICE` (25), and the static `LONG_BOUNDS` / `SHORT_BOUNDS` per ATR regime |
| `EXPIRY_TOO_CLOSE` triggers | `time_remaining_sec < 180` (3 min) |
| Why | Below 3 min, fills become unreliable and gamma blows up |

### `SHORT_ENTRY_TOO_CLOSE_TO_EXPIRY_*s<*s`

| | |
|---|---|
| What it blocks | Short entries when the contract has less than `short_min_seconds_to_expiry` left. Affects **both lanes**. Longs are unaffected — they trade fine in the close window |
| Source | `backend/filters/price_guard.py::is_allowed` (added 2026-04-28) |
| Config | `RiskConfig.short_min_seconds_to_expiry` env: `SHORT_MIN_SECONDS_TO_EXPIRY` (default `780` = 13 min) |
| Why | Paper attribution on 21d showed shorts entered with ≥13 min to close were 59% WR / +$1k net, while shorts entered with <13 min were 0–30% WR / **-$6.6k net across 27 trades** — dominated by `SHORT_SETTLEMENT_GUARD` blow-ups. Block on entry instead of panic-exiting after the loss has already accrued |
| Tune by | Lower the threshold gradually if attribution shows the 11–12 min cohort recovers. Raise it if the 13–15 min bucket also starts losing |
| Telemetry | `signal_log.skip_reason` will start showing `SHORT_ENTRY_TOO_CLOSE_TO_EXPIRY_*`. Watch for the absence of `SHORT_SETTLEMENT_GUARD` exits in `paper.exit` log events as confirmation the upstream block is working |

### `SHORT_SETTLEMENT_GUARD`

| | |
|---|---|
| What it does | Exit-side panic rule: if we hold a SHORT and `time_remaining_sec < 300` and current price > entry price (we're underwater), close the position immediately |
| Source | `backend/coordinator.py` lines ~370 |
| Config | `RiskConfig.short_settlement_guard_sec` env: `SHORT_SETTLEMENT_GUARD_SEC` (default `300` = 5 min) |
| Why | Short positions in the last few minutes of a Kalshi 15-min contract experience extreme settlement-time gamma. Without this guard, an underwater short can blow up to a near-100¢ exit |
| Historical rate (pre-2026-04-28 fix) | 16 paper exits in 14d — **0% win rate**, every single one a loss, total **-$3,147** |
| Status (2026-04-28) | Paired with `SHORT_ENTRY_TOO_CLOSE_TO_EXPIRY` on the entry side. Still kept as a defense-in-depth net for any short that slips through (e.g. config rollback or a slow-arriving signal). It should now be the rare exit reason, not a recurring one |

---

## Counterfactual: would widening `EDGE_PRICE_CAP` help?

Paper-trade analysis on the last 21 days of LONG entries by entry-price bucket
(filtered to exclude `data_quality_flag IN ('CATASTROPHIC_SHORT','CORRUPTED_PNL')`):

| Bucket | n | avg PnL | total PnL | WR % |
|---|---|---|---|---|
| ≤25c | 212 | +$235 | +$49,856 | 58.0 |
| 26–30c | 34 | +$81 | +$2,768 | 61.8 |
| 31–35c | 25 | +$44 | +$1,110 | 48.0 |
| 36–40c | 29 | -$56 | -$1,612 | **17.2** |
| 41–50c | 12 | +$18 | +$219 | 50.0 |
| >50c | 15 | -$73 | -$1,091 | 13.3 |

**Verdict:** Raising the cap from 25c → 30c is well-supported by 34 trades earning
+$2,768 paper. Raising further to 35c is marginal but still positive. **Above 35c
is a hard cliff** — go higher and you start losing money fast.

The OBI+ROC-agreement exemption is essentially never used today (no paper trades
in 26–35c had agreement signals), so it's not silently widening the cap behind
your back.

To raise the cap to 30c: `export EDGE_LIVE_MAX_ENTRY_PRICE=30.0` and redeploy.

---

## Known issues and current investigations

### Live trading silence (2026-04-27 onward)

The live lane has not opened a position in 24+ hours. Diagnosis (2026-04-28):

* The bot is healthy: WS connected, ATR not HIGH, signals are firing.
* Every signal that survives ATR/spread/trend gates is being rejected by:
  - `EDGE_SHORT_BLOCKED` (~3,500/week), or
  - `ML_GATE_REJECTED_*` (paper shadow only — does not affect live), or
  - There simply hasn't been a long signal that scored well enough.

This is **intentional** behavior given the current paper-side losses on shorts.
Don't unblock without a fix for the settlement-guard pattern below.

### `SHORT_SETTLEMENT_GUARD` was symptom, not cure (resolved 2026-04-28)

The 16 SHORT_SETTLEMENT_GUARD exits in the 14 days leading up to 2026-04-28 were
100% losses. Looking at the entry timing, every one of them was entered with
**5 minutes or less remaining** in the contract:

| min-to-close at entry | n shorts | avg PnL | total PnL | WR % | guard exits |
|---|---|---|---|---|---|
| 0–3 min | 9 | -$40 | -$364 | 56% | 3 |
| **4–5 min** | **14** | **-$196** | **-$2,747** | **0%** | **13** |
| 6–8 min | 1 | -$218 | -$218 | 0% | 0 |
| 9–12 min | 3 | -$1,097 | -$3,290 | 0% | 0 |
| **13–15 min** | **204** | **+$5** | **+$1,010** | **59%** | 0 |

Shorts entered with ≥13 min to close are 59% WR and net positive. Shorts entered
with <13 min were catastrophic (-$6,619 across 27 trades).

**Fix shipped 2026-04-28:** new `SHORT_ENTRY_TOO_CLOSE_TO_EXPIRY` guard in
`backend/filters/price_guard.py` blocks short entries when `time_remaining_sec
< 780` (13 min). See the "SHORT_ENTRY_TOO_CLOSE_TO_EXPIRY" filter section above.

Validation plan: after 7 days of paper trading with the new guard, expect:

* Zero or near-zero `SHORT_SETTLEMENT_GUARD` exits in `paper.exit` log events
* `signal_log.skip_reason` shows `SHORT_ENTRY_TOO_CLOSE_TO_EXPIRY_*` rejections
  in the same time window where the guard used to fire
* Net short PnL on paper improves by approximately the historical loss rate
  (~$3.1k / 14d, scaled to whatever the next window covers)

### Pattern: re-entering shorts immediately after a guard exit

Concrete example (2026-04-28 13:49–13:58 UTC):

1. 13:49:48 — enter SHORT 2624 contracts of `B76350` at 26¢ (~10 min to close)
2. 13:56:10 — guard fires (price 39¢, remaining 229s). Loss: -$420
3. 13:56:18 — *new* SHORT 1734 contracts of `B76050` at 39¢ (~3:42 to close)
4. 13:58:10 — guard fires again (price 59.5¢, remaining 109s). Loss: -$413

The bot doubled its loss in 8 minutes by re-entering. A no-short-in-final-15-min
rule would have prevented both.

---

## Operator playbook: tuning a filter

1. Identify the filter you want to tune from the table above. Note the env var.
2. Run a counterfactual on paper data first — the SQL pattern is in this runbook.
3. If the counterfactual supports the change, update the env var on the prod
   `.env` file (NOT in source control), then redeploy with `bash scripts/deploy.sh`.
4. Watch `signal_log.skip_reason` for 24 h to confirm the new rate looks sane.
5. Wait at least one weekly attribution cycle before deciding whether the change
   is sticking.

---

## Expiry Exit Reliability (2026-05-04 program)

This four-phase program addresses the divergence between paper EXPIRY_GUARD wins
and live EXPIRY_GUARD orphan losses observed on 2026-05-04. **Phases 1-3 ship
with code; Phase 4 ships with telemetry only and remains deferred behind an
explicit operator activation.**

### Phase 1 — Realistic paper guard fills (LIVE)

| | |
|---|---|
| What changed | Paper EXPIRY_GUARD / SHORT_SETTLEMENT_GUARD now use the executable side of the book (`best_yes_bid` for long, `100 - best_yes_ask` for short) instead of `OrderBookState.mid`. Missing executable side → no synthetic fill is recorded; settlement closes the trade. |
| Source | `backend/coordinator.py::_get_executable_exit_price_for`, `_run_settlement_guards` paper branch |
| Telemetry | `paper.exit` log events carry `fill_source="paper_guard_taker_bidask"` for the new path. Legacy synthetic fills get `fill_source="paper_mid_mark"`. The `trades.fill_source` column distinguishes the two. |
| Verification SQL | `SELECT fill_source, COUNT(*), AVG(pnl), AVG(exit_price - entry_price) FROM trades WHERE trading_mode='paper' AND exit_reason IN ('EXPIRY_GUARD','SHORT_SETTLEMENT_GUARD') AND timestamp > NOW() - INTERVAL '7 days' GROUP BY fill_source;` |
| Expected effect | Paper EXPIRY_GUARD win rate drops from ~91% (synthetic mid) to within ±10% of live (executable side). PnL drops accordingly; this is the correct counterfactual for live. |

### Phase 2 — Live retry widening (LIVE, but disabled by default)

| | |
|---|---|
| What changed | The coordinator now passes a 1-based retry attempt index into `LiveTrader.exit(price, reason, attempt=N)`, which threads it into `PositionManager._exit_inner`. For `EXPIRY_GUARD` / `SHORT_SETTLEMENT_GUARD` only, `_compute_expiry_retry_floor` picks an attempt-specific `yes_price` / `no_price` order-side floor. Backoff and max-attempts are configurable. |
| Source | `backend/coordinator.py::_handle_live_exit`, `backend/execution/{live_trader.py,position_manager.py}`, `BotConfig.expiry_retry_*` |
| Defaults | `EXPIRY_RETRY_FIRST_ATTEMPT_YES_FLOOR_CENTS=1`, `EXPIRY_RETRY_WIDEN_STEP_CENTS=0`, `EXPIRY_RETRY_FINAL_ATTEMPT_MAX_AGGRESSIVE=true`. With defaults the order pricing is **identical to pre-Phase-2** (1c floor, max aggressive). |
| Opt-in example | Set `EXPIRY_RETRY_FIRST_ATTEMPT_YES_FLOOR_CENTS=30` and `EXPIRY_RETRY_WIDEN_STEP_CENTS=10` to try a 30c → 20c → 1c (final pin) schedule across the default 3 attempts. |
| Safety rail | The final attempt is always pinned to 1c when `EXPIRY_RETRY_FINAL_ATTEMPT_MAX_AGGRESSIVE=true`. Do not disable this in production without a corresponding paper soak. |
| Telemetry | `position_manager.expiry_retry_floor` log event records `attempt`, `floor_cents`, `side`, `reason`. Cross-reference against `trades.exit_reason`. |
| Expected effect | When opted-in, a fraction of EXPIRY_GUARD round-trips harvest better fills; the rest fall through to the existing 1c-floor behavior on the final attempt. **No worse-case orphan exposure** because the final attempt is unchanged. |

### Phase 3 — Pre-expiry passive limit ladder (CODED, default OFF)

| | |
|---|---|
| What it does | When `time_remaining_sec < ladder_start_trigger_sec` AND >= `expiry_guard_trigger_sec`, places passive limit exits at progressively-aggressive rungs inside the spread. Each rung is canceled and stepped if not filled within `LADDER_RUNG_TIMEOUT_SEC`. Total budget is bounded by `LADDER_TOTAL_BUDGET_SEC`. **Always falls back to EXPIRY_GUARD on residual or timeout — never extends the orphan window.** |
| Source | `backend/execution/position_manager.py::try_passive_limit_ladder`, `backend/coordinator.py::_run_pre_expiry_ladder` |
| Config | `LADDER_ENABLED_PAPER`, `LADDER_ENABLED_LIVE`, `LADDER_START_TRIGGER_SEC=240`, `LADDER_TOTAL_BUDGET_SEC=50`, `LADDER_RUNG_COUNT=3`, `LADDER_RUNG_FIRST_OFFSET_CENTS=5`, `LADDER_RUNG_STEP_CENTS=3`, `LADDER_RUNG_TIMEOUT_SEC=8.0` |
| Defaults | Both paper and live ladder flags **off**. The ladder is dormant until an operator opts in. |
| Paper-mode caveat | The paper trader has no order-book simulation for resting limit orders, so the paper ladder flag is reserved for a future enhancement and currently does not change paper exit behavior. Use the canary live environment (`KALSHI_ENV=demo`) to soak the ladder before flipping the production live flag. |
| Telemetry | `/api/diagnostics` exposes `expiry_ladder.{telemetry,config}`; `/api/status` exposes `expiry_ladder` (telemetry only). Counters: `runs`, `full_fills`, `partial_fills`, `no_fills`, `fallbacks`. |
| Rollout discipline | (1) Enable on canary (demo Kalshi) for ≥7 days; verify diagnostics counters match expectations and no `position_manager.ladder_cancel_failed` storms. (2) Enable live with `LIVE_TRADE_LIMIT` ≤ 5 for ≥7 days. (3) Compare live EXPIRY_GUARD outcomes pre/post: ladder full-fill rate ≥ 30% AND no-fill ratio ≤ ladder-disabled rate. |
| Restart safety | Default `LADDER_CANCEL_ON_RESTART=false`. The reconciliation path will surface stale ladder orders as orphans on the next tick if the bot restarts mid-ladder. The `LADDER_CANCEL_ON_RESTART=true` setting is a future enhancement; do not enable until verified on demo. |

### Phase 4 — Deferred orphan/depth gates (TELEMETRY ONLY)

**Status:** No execution behavior change. Counters are recorded so we can decide
whether to enable real gating later.

| Counter | What it measures | Activation rubric |
|---|---|---|
| `orphan_break_even_observed` | Every orphan-check pass with a usable bid. | Denominator. |
| `orphan_break_even_blocked` | Subset where `bid < orphan.avg_entry_price` (current behavior already blocks these). | Numerator for the orphan-loss-tolerance proposal. **Activate** orphan-tolerance widening if blocked / observed > 0.40 over a rolling 14-day window AND the average loss-if-acted is < $0.20/contract. |
| `near_expiry_depth_observed` | Hypothetical entry-depth observations near expiry; tagged at decision time, never enforced. | Denominator. |
| `near_expiry_depth_would_block` | Subset where the book thickness at entry price would have failed the proposed gate. | **Activate** entry-depth gating only after running a paper counterfactual that shows the rejected cohort is net-negative AND `would_block / observed` < 0.10 (so it doesn't kill the long-side edge). |

| | |
|---|---|
| Source | `backend/execution/position_manager.py::{record_entry_depth_observation,get_phase4_telemetry}` |
| Where surfaced | `/api/diagnostics` -> `phase4_deferred_gates.telemetry` |
| Owner signoff for activation | Quant + SRE must both sign off in writing on the rubric thresholds before any flag is flipped from telemetry-only to enforcing. The dashboard column for these counters MUST stay labeled "telemetry-only" until that signoff. |

### Rollout sequence (operator playbook)

Step A — Phase 1 + Phase 2 with ladder OFF:

1. From local: `bash scripts/deploy.sh` (rsync now excludes ML artifacts; see BUG-033).
2. Apply the schema migration on the remote DB (the trades.fill_source widening from VARCHAR(20)→VARCHAR(40) is required for the new `paper_guard_taker_bidask` label):
   ```sh
   ssh "$KBTC_DEPLOY_HOST" "docker exec -i kbtc-db psql -U kalshi -d kbtc" \
     < backend/migrations/011_widen_fill_source.sql
   ```
3. Restart the container to pick up the new code: `ssh "$KBTC_DEPLOY_HOST" "cd /home/botuser/kbtc && docker compose up -d --build"`.
4. Verify health: `ssh "$KBTC_DEPLOY_HOST" "curl -s 'http://localhost:8000/api/status'"` and confirm `expiry_ladder.config.live_enabled=false`.

Step B — Optional retry widening soak (Phase 2 opt-in):

1. Set `EXPIRY_RETRY_FIRST_ATTEMPT_YES_FLOOR_CENTS=30`, `EXPIRY_RETRY_FIRST_ATTEMPT_NO_FLOOR_CENTS=30`, `EXPIRY_RETRY_WIDEN_STEP_CENTS=10` in the remote `.env` (keep `EXPIRY_RETRY_FINAL_ATTEMPT_MAX_AGGRESSIVE=true`).
2. Restart and observe `position_manager.expiry_retry_floor` log events for ≥3 trade days.
3. Compare EXPIRY_GUARD success vs ORPHAN_SETTLED ratios using the verification SQL.

Step C — Ladder canary (Phase 3 opt-in, demo Kalshi only):

1. On a canary droplet with `KALSHI_ENV=demo` and `LIVE_TRADE_LIMIT` ≤ 5, set `LADDER_ENABLED_LIVE=true`.
2. Run for ≥7 days. Read `/api/diagnostics` daily; counters should show `runs > 0`, `fallbacks ≤ runs`, no `ladder_cancel_failed` log spam.
3. Promote to production only after the canary report passes.

Step D — Phase 4 stays telemetry-only:

1. The deferred-gate counters at `/api/diagnostics` -> `phase4_deferred_gates` accumulate as live trades happen.
2. Do NOT flip any execution behavior on these counters until the rubric thresholds above are met AND signed off by Quant + SRE.

### Verification SQL

A canned script lives at `scripts/expiry_exit_reliability_verify.sql`. Run it post-deploy:

```sh
ssh "$KBTC_DEPLOY_HOST" "docker exec -i kbtc-db psql -U kalshi -d kbtc" \
  < scripts/expiry_exit_reliability_verify.sql \
  | tee backend/backtest_reports/expiry_verify_$(date +%Y%m%d).txt
```

Quick spot-checks (also embedded in the script):

```sql
-- Phase 1: paper fill_source distribution for guard exits
SELECT fill_source,
       COUNT(*) AS n,
       ROUND(AVG(pnl)::numeric, 2) AS avg_pnl,
       ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) AS win_pct
FROM trades
WHERE trading_mode = 'paper'
  AND exit_reason IN ('EXPIRY_GUARD', 'SHORT_SETTLEMENT_GUARD')
  AND timestamp > NOW() - INTERVAL '14 days'
  AND data_quality_flag IS NULL
GROUP BY fill_source
ORDER BY n DESC;

-- Phase 2: live EXPIRY_GUARD outcome distribution by retry-attempt
-- Cross-reference structured logs (position_manager.expiry_retry_floor)
-- with trade rows for the same ticker.

-- Phase 3: ladder counters (process-local, read /api/diagnostics)

-- Phase 4: telemetry counters (process-local, read /api/diagnostics)
```

---

## SQL snippets

```sql
-- Last 7d skip-reason distribution (live + paper, since signal_log isn't mode-tagged)
SELECT
  CASE
    WHEN skip_reason LIKE 'SHORT_BLOCKED_UPTREND%' THEN 'SHORT_BLOCKED_UPTREND_*'
    WHEN skip_reason LIKE 'EDGE_PRICE_CAP%' THEN 'EDGE_PRICE_CAP_*'
    WHEN skip_reason LIKE 'ML_GATE_REJECTED%' THEN 'ML_GATE_REJECTED_*'
    WHEN skip_reason LIKE 'EDGE_HOUR_BLOCKED%' THEN 'EDGE_HOUR_BLOCKED_*'
    WHEN skip_reason LIKE 'EDGE_DRIVER_BLOCKED%' THEN 'EDGE_DRIVER_BLOCKED_*'
    ELSE skip_reason
  END AS reason_class,
  COUNT(*) AS n
FROM signal_log
WHERE timestamp > NOW() - INTERVAL '7 days'
  AND skip_reason IS NOT NULL
  AND skip_reason <> 'NO_SIGNAL'
GROUP BY reason_class
ORDER BY n DESC;

-- Paper LONG performance by entry-price bucket (run this before changing EDGE_LIVE_MAX_ENTRY_PRICE)
SELECT
  CASE
    WHEN entry_price <= 25 THEN '<=25c'
    WHEN entry_price <= 30 THEN '26-30c'
    WHEN entry_price <= 35 THEN '31-35c'
    WHEN entry_price <= 40 THEN '36-40c'
    WHEN entry_price <= 50 THEN '41-50c'
    ELSE '>50c'
  END AS price_bucket,
  COUNT(*) AS n_trades,
  ROUND(AVG(pnl)::numeric, 2) AS avg_pnl,
  ROUND(SUM(pnl)::numeric, 2) AS total_pnl,
  ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) AS win_pct
FROM trades
WHERE trading_mode = 'paper'
  AND direction = 'long'
  AND timestamp > NOW() - INTERVAL '21 days'
  AND (data_quality_flag IS NULL OR data_quality_flag NOT IN ('CATASTROPHIC_SHORT','CORRUPTED_PNL'))
GROUP BY price_bucket
ORDER BY MIN(entry_price);

-- Paper SHORT performance by minutes-to-close at entry (the SHORT_SETTLEMENT_GUARD diagnostic)
WITH parsed AS (
  SELECT id, ticker, timestamp, pnl, exit_reason, entry_price, contracts,
         (15 - (EXTRACT(MINUTE FROM timestamp)::int % 15)) AS min_to_close_approx
  FROM trades
  WHERE trading_mode = 'paper' AND direction = 'short'
    AND timestamp > NOW() - INTERVAL '21 days'
)
SELECT
  CASE
    WHEN min_to_close_approx <= 3  THEN '0-3 min to close'
    WHEN min_to_close_approx <= 5  THEN '4-5 min'
    WHEN min_to_close_approx <= 8  THEN '6-8 min'
    WHEN min_to_close_approx <= 12 THEN '9-12 min'
    ELSE '13-15 min'
  END AS bucket,
  COUNT(*) AS n_trades,
  ROUND(AVG(pnl)::numeric, 2) AS avg_pnl,
  ROUND(SUM(pnl)::numeric, 2) AS total_pnl,
  ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) AS win_pct,
  COUNT(*) FILTER (WHERE exit_reason = 'SHORT_SETTLEMENT_GUARD') AS guard_exits
FROM parsed
GROUP BY bucket
ORDER BY MIN(min_to_close_approx);
```
