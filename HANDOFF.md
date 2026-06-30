# KBTC Bot — Handoff & Resume Guide

> **For AI agents and operators picking this project back up.**
> Last updated: 2026-06-30. Bot was paused intentionally at this date.

---

## 1. What This Bot Does

Automated prediction-market trading bot running 24/7 on a DigitalOcean droplet.
It trades Kalshi's BTC 15-minute binary contracts using:

- **OBI** (Order Book Imbalance): if bid pressure > 68% of total book, signal is long
- **ROC** (Rate of Change): 3-candle momentum confirms direction
- **ATR regime filter**: blocks all entries during HIGH volatility
- **XGBoost ML gate**: trained weekly on `trade_features`, screens out low-probability setups
- **Kelly sizer**: position size = 25% Kelly × 5% max position pct × current bankroll
- **Hard stop loss**: 2% (`STOP_LOSS_PCT=0.02`), profit target = 2× stop (`PROFIT_TARGET_MULT=2.0`)

The bot has two parallel modes running simultaneously:
- **Paper** (simulated fills, $1K start) — always on, generates training data, no real money
- **Live** (real Kalshi API orders) — currently paused, bankroll $96.67, LIVE_TRADE_LIMIT=20

---

## 2. Infrastructure

### Server

| Item | Value |
|------|-------|
| Provider | DigitalOcean |
| Host | `botuser@167.71.247.154` |
| SSH | `ssh botuser@167.71.247.154` |
| Dashboard URL | `http://167.71.247.154:8000` |
| Dashboard API token | `QLKO-LZkwdy4sxT_LGzWvCljzSZ7uqvwmDVeLPYI0Xs` |

### Local environment variable (set this in your shell before running any scripts)

```bash
export KBTC_DEPLOY_HOST=botuser@167.71.247.154
```

### Containers (Docker Compose)

| Container | Status when paused | Purpose |
|-----------|-------------------|---------|
| `kbtc-bot` | **STOPPED** | Main Python bot + FastAPI dashboard |
| `kbtc-db` | **RUNNING** | PostgreSQL 16 + TimescaleDB — do not stop, data lives here |

### Key paths on remote server

```
/home/botuser/kbtc/          # project root (rsynced from local)
/home/botuser/kbtc/.env      # all config and secrets (source of truth)
/home/botuser/kbtc/logs/     # cron logs, sweep logs
/home/botuser/kbtc/backend/ml/models/  # XGBoost model artifacts
/home/botuser/kbtc/backend/ml/models/.promotion_log.json  # ML promotion history
```

---

## 3. How to Restart the Bot

```bash
# From local machine:
ssh "$KBTC_DEPLOY_HOST" "cd /home/botuser/kbtc && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-build bot"

# Verify it came up:
ssh "$KBTC_DEPLOY_HOST" "curl -s http://localhost:8000/api/status | python3 -c \"import json,sys; d=json.load(sys.stdin); print('paper_bankroll:', d.get('paper_bankroll')); print('trading_paused:', d.get('trading_paused'))\""
```

The bot auto-loads the latest promoted ML model on startup. No extra steps needed after restart.

### After any code or config change

1. If frontend files changed: `cd frontend && npm run build && cp -r dist/* ../backend/static/`
2. Deploy: `bash scripts/deploy.sh` (rsyncs, rebuilds Docker image, restarts)
3. Verify: `ssh "$KBTC_DEPLOY_HOST" "curl -s http://localhost:8000/api/status"`

### How to stop (pause) safely

```bash
# Stop bot only — DB keeps running, all data preserved
ssh "$KBTC_DEPLOY_HOST" "cd /home/botuser/kbtc && docker compose stop bot"
```

---

## 4. Current State as of 2026-06-30

### Paper trading performance

| Metric | Value |
|--------|-------|
| Paper bankroll | $18.7M (Kelly-compounded from $1K start Apr 13) |
| All-time trades | 3,292 |
| All-time win rate | 56.4% |
| All-time PnL | +$18.8M (paper) |
| Last 14d win rate | **71% (OBI), 68% (OBI+ROC)** |
| Last 14d avg PnL/trade | ~$28K (inflated by Kelly compounding) |
| Last 7d trades | 311 |
| Stop rate (14d) | ~12% of trades |

**Important caveat:** The $18.7M paper bankroll is a simulation artifact. The Kelly sizer compounds
position sizes with the bankroll — with $18.7M and 5% max position, the bot is sizing
**1.6–2.1 million contracts per trade**. Kalshi's real market depth supports hundreds, not millions.
The *win rates and signal quality* are real and meaningful; the dollar amounts are not.

### Weekly performance trend

| Week | Trades | Win Rate | Total PnL |
|------|--------|----------|-----------|
| Jun 29 | 52 | 84.6% | $8.45M |
| Jun 22 | 343 | 67.6% | $9.19M |
| Jun 15 | 317 | 69.4% | $565K |
| Jun 8 | 243 | 63.4% | $48K |
| Jun 1 | 140 | 50.0% | $5K |

The step-change improvement after Jun 18 came from reverting `STOP_LOSS_PCT` back to `0.02`
(from `0.30` which someone had set in May). Tight stops preserve the TAKE_PROFIT pathway.

### Live trading

Currently **paused**. Live bankroll: $96.67. The `LIVE_TRADE_LIMIT=20` cap and `SUPERVISED_AUTO_PAUSE=false` are in effect. Live was paused historically because of `400 Bad Request` errors from Kalshi when trying to size orders against the low live bankroll.

### ML model

| Item | Value |
|------|-------|
| Current model | `xgb_entry_v1.pkl` |
| Promoted | 2026-06-28 |
| OOS Precision | **0.670** (↑ from 0.656, ↑ from 0.638, ↑ from 0.627) |
| Training rows | 3,077 |
| Threshold | 0.3457 |
| Gate mode | Paper-only shadow (`ML_GATE_PAPER=true`, `ML_GATE_LIVE=false`) |
| Precision trend | 5 straight weeks of improvement (0.59 → 0.67) |

---

## 5. Outstanding To-Dos

### High priority (do these before resuming live trading)

**A. Kelly position size dollar cap**

The Kelly sizer has no absolute dollar ceiling — it only caps at `KELLY_MAX_POSITION_PCT=0.05`
(5% of bankroll). With an $18.7M paper bankroll this means ~$940K per simulated trade.

Fix: add a `KELLY_MAX_DOLLARS` env var (e.g. `KELLY_MAX_DOLLARS=5000`) and enforce it in
`backend/risk/kelly_sizer.py`. This keeps paper realistic and is critical before any live scaling.

**B. Memory leak — RSS watchdog restarts every ~6 hours**

The bot was restarting every ~6 hours because `backend/backtesting/data_loader.py` was being
called inside the live process (via the historical sync module), loading 90 days of OB snapshots
on every 10-minute sync cycle, pushing RSS above the 1,200 MiB limit.

Side effect: `bg_persist_dropped: ~12,000` OB snapshots dropped per cycle = training data loss.

Two-part fix:
1. **Quick**: In `.env`, reduce `PREDEXON_BOOTSTRAP_DAYS=90` → `30` (cuts candle load by 67%)
2. **Proper**: Investigate why `data_loader.py` memory isn't released between sync cycles.
   The `historical_sync` module likely holds a reference preventing GC.

**C. OBI threshold sweep — fix before using for parameter decisions**

The `scripts/backtest_obi_threshold_sweep.py` sweep runs the backtester in `paper` mode, which
causes the edge profile filters (`EDGE_PAPER_LONG_ONLY`, `EDGE_PAPER_BLOCKED_HOURS_UTC`) to be
**skipped entirely** (they only fire in `live` mode, see `contract_backtester.py:962`). This means
~13% of backtest trades are shorts that are blocked in production, making results incomparable.

Fix: add `long_only=True` and `blocked_hours_utc=[14, 17]` to `base_cfg` in the sweep script,
or add a `long_only` override key to the contract backtester.

### Medium priority

**D. ML gate promotion to live**

`ML_GATE_LIVE=false` — the XGBoost model runs in shadow mode on paper only. As precision
continues to improve (currently 0.670), consider enabling it on live once paper shadow-mode
comparison shows it's filtering bad entries without hurting good ones.

Check `backend/ml/models/.promotion_log.json` for the full history.

**E. Live trading re-evaluation**

Live trading is paused with $96.67 bankroll (too small to size orders without hitting Kalshi's
minimum contract sizes). Before re-enabling:
1. Decide on a new live bankroll (deposit more or start fresh)
2. Confirm the orphan safety canary passes (see `scripts/PROMOTION_GATES.md`)
3. Verify `LIVE_TRADE_LIMIT` is set to a safe supervised cap

---

## 6. Key Configuration (current `.env` on remote)

```
# Strategy
OBI_LONG_THRESHOLD=0.68       # entry threshold (was 0.65, raised Jun 9)
OBI_SHORT_THRESHOLD=0.32
OBI_CONSECUTIVE_READINGS=2
ROC_LOOKBACK=3
ROC_LONG_THRESHOLD=0.4

# Risk
STOP_LOSS_PCT=0.02             # ← reverted Jun 18 (was 0.30, then 0.05)
PROFIT_TARGET_MULT=2.0         # 2× stop = take profit at +4%
RISK_PER_TRADE_PCT=0.02
KELLY_FRACTION=0.25
KELLY_MAX_POSITION_PCT=0.05    # ← needs a dollar cap added (see To-Do A)
DAILY_LOSS_LIMIT_PCT=0.06
WEEKLY_LOSS_LIMIT_PCT=0.15

# Edge profile (paper and live both long-only)
EDGE_LIVE_LONG_ONLY=true
EDGE_PAPER_LONG_ONLY=true
EDGE_LIVE_BLOCKED_HOURS_UTC=14,17
EDGE_PAPER_BLOCKED_HOURS_UTC=14,17

# ML gate
ML_GATE_ENABLED=true
ML_GATE_PAPER=true             # shadow mode on paper
ML_GATE_LIVE=false             # not yet live

# Memory (needs tuning — see To-Do B)
BOT_MEM_LIMIT_MB=1200
PREDEXON_BOOTSTRAP_DAYS=90     # ← reduce to 30 (causes RSS OOM)

# Live trading
TRADING_MODE=paper             # bot runs paper; live requires explicit enable
LIVE_TRADE_LIMIT=20
```

---

## 7. Automated Cron Schedule (runs on remote server)

All crons run as `botuser` on the droplet host (not inside Docker).

| Schedule | Script | Log | Purpose |
|----------|--------|-----|---------|
| Sunday 04:00 UTC | `retrain_xgb_cron.sh` | `logs/retrain_xgb.log` | XGBoost weekly retrain + auto-promote if precision improves |
| Sunday 05:00 UTC | `edge_profile_review_cron.sh` | `logs/edge_review.log` | Edge profile parameter review (14-day window) |
| Sunday 05:30 UTC | `edge_profile_apply_cron.sh` | `logs/edge_apply.log` | Auto-apply Tier 1 recommendations (disabled — `EDGE_LIVE_AUTO_APPLY_ENABLED=false`) |
| Daily 03:00 UTC | `attribution_backfill_cron.sh` | `logs/attribution_backfill.log` | Nightly PnL attribution backfill |
| Daily 09:00 UTC | `canary_report_cron.sh` | `logs/canary_report.log` | Orphan safety canary health check |

**Note:** Crons that run against the DB (retrain, edge review) work even when the bot container
is stopped because they connect directly to `kbtc-db`. They will continue running on schedule
while the bot is paused.

---

## 8. Connecting & Useful Commands

```bash
# Set up (one-time, in your shell profile)
export KBTC_DEPLOY_HOST=botuser@167.71.247.154

# SSH into server
ssh "$KBTC_DEPLOY_HOST"

# View bot logs (live)
ssh "$KBTC_DEPLOY_HOST" "docker logs -f kbtc-bot"

# View recent paper trades
ssh "$KBTC_DEPLOY_HOST" "docker exec kbtc-db psql -U kalshi -d kbtc -c \"
SELECT DATE(timestamp), COUNT(*), ROUND(SUM(pnl)::numeric,2), ROUND(AVG(pnl)::numeric,2),
  ROUND(COUNT(*) FILTER (WHERE pnl>0)::numeric/COUNT(*)::numeric*100,1) AS wr
FROM trades WHERE trading_mode='paper' AND timestamp > NOW() - INTERVAL '14 days'
GROUP BY 1 ORDER BY 1 DESC;\""

# Check bot health
ssh "$KBTC_DEPLOY_HOST" "curl -s http://localhost:8000/api/status | python3 -m json.tool"

# View ML model state
ssh "$KBTC_DEPLOY_HOST" "cat /home/botuser/kbtc/backend/ml/models/xgb_entry_v1_meta.json | python3 -m json.tool"

# View edge profile change history
ssh "$KBTC_DEPLOY_HOST" "docker exec kbtc-db psql -U kalshi -d kbtc -c \"
SELECT changed_at, param, old_value, new_value, applied_by, notes
FROM edge_profile_change_log ORDER BY changed_at DESC LIMIT 10;\""

# Check RSS memory usage (look for rss_watchdog events)
ssh "$KBTC_DEPLOY_HOST" "docker logs kbtc-bot 2>&1 | grep rss_watchdog_threshold | tail -5"

# Deploy after local code changes
bash scripts/deploy.sh

# Run a backtest sweep on the remote container (example)
ssh "$KBTC_DEPLOY_HOST" "docker exec kbtc-bot python3 /tmp/scripts/backtest_stop_loss_sweep.py \
  --start 2026-06-01 --end 2026-06-30 --bucket-sec 15 --bankroll 10000 \
  --output /tmp/sl_sweep_latest.json"
```

---

## 9. Known Issues and Bugs

See `.cursor/rules/known-bugs.mdc` for the full annotated log. Active issues as of 2026-06-30:

| Bug | Status | Notes |
|-----|--------|-------|
| Memory leak (RSS ~6h restart) | **Active** | `data_loader.py` in historical sync holds refs; quick fix: reduce `PREDEXON_BOOTSTRAP_DAYS=30` |
| Kelly compounding unrealistic | **Active** | No dollar cap on position size; add `KELLY_MAX_DOLLARS` to sizer |
| OBI threshold sweep includes shorts | **Active** | Backtester in paper mode skips edge profile; sweep results not production-comparable |
| `bg_persist_dropped` | Active side-effect of memory leak | OB snapshots dropped when queue fills before restart; training data has gaps |

---

## 10. Key Design Decisions Made During Build

These are non-obvious decisions that changed from defaults — important context for the next agent:

1. **Long-only on both paper and live** (`EDGE_PAPER_LONG_ONLY=true`): Set May 11.
   Shorts were consistently 0–15% win rate in attribution. Paper is long-only to keep
   training data clean and avoid polluting the ML model with unprofitable patterns.

2. **STOP_LOSS_PCT=0.02 is the correct value**: The May 11 operator had widened this to 0.30
   (thinking it would improve WR by riding out drawdowns). Backtesting on May 25–Jun 8 data
   confirmed 0.02 is strictly best across all thresholds. Reverted Jun 18.

3. **OBI threshold is 0.68, not 0.65**: Raised Jun 9 based on analysis showing 0.65 was at breakeven.
   The Jun 18 sweep to validate 0.70 was inconclusive because the backtester doesn't filter directions
   correctly (see To-Do C). Hold at 0.68 until the sweep is fixed.

4. **Blocked hours 14 and 17 UTC**: Hour 17 is 11am US/East (US equity open) — OBI signals are
   unreliable during this period. Both live and paper block these hours.

5. **ML gate in shadow mode only**: The model is trained weekly but only runs as a shadow gate
   on paper (it logs what it would filter but doesn't actually block entries). Once OOS precision
   is consistently ≥ 0.70, consider enabling `ML_GATE_PAPER=true` as a real gate (not just shadow).
   Then `ML_GATE_LIVE=true` after paper validation.

6. **PROFIT_TARGET_MULT=2.0**: Was at 1.50 (original), then destroyed (set to 0.10 in May 11),
   now at 2.0. 2× stop means if stop=2%, TP fires at +4%. This is working well — see high TP rates.

7. **EXIT_INTELLIGENCE_SHADOW_ONLY=false**: The exit intelligence system (health score monitoring,
   candle reversal, momentum stall detection) is fully live on paper. It contributed to the
   improvement in win rates by avoiding holding losing positions to expiry.

---

## 11. Project File Map (most important files for agent work)

```
backend/
  coordinator.py          — main event loop, signal evaluation, entry/exit logic
  strategies/
    obi.py               — OBI signal + exit check logic
    roc.py               — ROC momentum signal
    resolver.py          — signal conflict resolution (OBI+ROC agreement)
  filters/
    atr_regime.py        — ATR volatility gate
    edge_profile.py      — live/paper edge profile filters
    spread_regime.py     — spread divergence modifier
  risk/
    kelly_sizer.py       — position sizing (← add KELLY_MAX_DOLLARS here)
    circuit_breaker.py   — daily/weekly/drawdown halt logic
  execution/
    position_manager.py  — live position state machine
    paper_position_manager.py — paper position state machine
  backtesting/
    contract_backtester.py   — main backtest engine
    data_loader.py           — DB data loading for backtests (← memory leak here)
  ml/
    inference.py         — XGBoost inference wrapper
    models/              — model artifacts + promotion log
  monitoring/
    live_health.py       — edge profile tripwires and alarms
scripts/
  deploy.sh                        — rsync + docker rebuild + restart
  backtest_stop_loss_sweep.py      — stop loss parameter sweep
  backtest_obi_threshold_sweep.py  — OBI threshold sweep (needs direction fix)
  retrain_xgb_cron.sh             — weekly XGB retrain cron
  edge_profile_review_cron.sh     — weekly edge profile review cron
  train_xgb.py                    — manual ML training script
.cursor/rules/                    — AI agent domain rules (auto-loaded by Cursor)
  known-bugs.mdc                  — running bug log
  backtesting-framework.mdc       — backtest conventions
  obi-trading.mdc                 — OBI strategy spec
  roc-trading.mdc                 — ROC strategy spec
  edge-profile-maintenance.mdc    — edge profile review process
```

---

## 12. How to Get Context Fast (for a new AI agent)

1. Read this file (`HANDOFF.md`) — you're doing that now
2. Read the Cursor rules in `.cursor/rules/` — they load automatically in Cursor and contain the full strategy spec, risk rules, and coding conventions
3. Run the status check: `ssh "$KBTC_DEPLOY_HOST" "curl -s http://localhost:8000/api/status | python3 -m json.tool"`
4. Check recent paper trade performance (SQL in section 8)
5. Read the latest ML promotion log: `cat /home/botuser/kbtc/backend/ml/models/.promotion_log.json`
6. Read the latest edge review report: `tail -100 /home/botuser/kbtc/logs/edge_review.log`

The Cursor agent transcript history for this project lives in the agent-transcripts folder in the Cursor project directory. Searching past transcripts by keyword (e.g. "STOP_LOSS", "OBI threshold", "memory leak") is the fastest way to understand *why* decisions were made.

---

*This document should be updated whenever the bot is paused or major decisions are made.*
