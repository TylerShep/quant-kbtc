# KBTC -- Kalshi BTC 15-Minute Trading Bot

Automated trading bot for Kalshi's BTC 15-minute prediction markets. Combines Order Book Imbalance (OBI) and Rate of Change (ROC) momentum signals with ATR-based volatility regime filtering, automated risk management, and a real-time dashboard.

## Architecture

```
Coinbase Spot WS ──┐
                    ├─→ Coordinator ──→ Strategies ──→ Resolver ──→ Execution
Kalshi Order Book ──┘        │              │                          │
                             │         ATR Regime                Paper / Live
                             │           Filter                    Trader
                             ▼
                    ┌─── Dashboard ───┐
                    │  Equity Chart   │
                    │  BTC Price      │
                    │  Signals/OBI    │
                    │  Trade History  │
                    │  Attribution    │
                    │  Backtest Viz   │
                    │  System Health  │
                    └─────────────────┘
```

### Core Components

| Directory | Purpose |
|-----------|---------|
| `backend/strategies/` | OBI and ROC signal generators, signal conflict resolver |
| `backend/filters/` | ATR volatility regime filter (gates entries in HIGH regimes) |
| `backend/risk/` | Position sizer (fixed fractional) and circuit breaker (daily/weekly/drawdown limits) |
| `backend/execution/` | Paper trader (simulated fills) and live trader (Kalshi REST API) |
| `backend/data/` | Kalshi WebSocket, Coinbase spot feed, candle aggregator |
| `backend/backtesting/` | Simulation engine, walk-forward optimizer, auto-tuner, attribution, reports |
| `backend/monitoring/` | Signal health/decay monitoring (IC, win rate drift, Sharpe drift) |
| `backend/api/` | FastAPI REST endpoints and WebSocket feed for the dashboard |
| `frontend/` | React + TypeScript + Tailwind CSS dashboard with TradingView charts |

### Signal Flow

1. **Data ingestion** -- Coinbase spot price + Kalshi order book stream into the coordinator
2. **ATR regime check** -- If volatility is HIGH, block all new entries
3. **OBI evaluation** -- Order book imbalance above 0.65 = bullish, below 0.35 = bearish
4. **ROC evaluation** -- 15-minute price rate of change confirms momentum direction
5. **Resolver** -- OBI + ROC must agree or at least not conflict; conviction level set (HIGH/NORMAL/LOW)
6. **Position sizing** -- Fixed fractional sizing scaled by conviction and drawdown state
7. **Execution** -- Paper trader records simulated fill; live trader places real Kalshi orders

## Quick Start

### Prerequisites

- Docker and Docker Compose
- Node.js 18+ (for frontend development)
- Kalshi API key with RSA private key
- Python 3.11+ (for local backtesting)

### Local Development

```bash
# Clone and configure
cp .env.example .env
# Edit .env with your Kalshi API credentials and Discord webhooks

# Start database and bot
docker compose up -d

# Frontend development server (hot reload)
cd frontend && npm install && npm run dev
```

The dashboard is available at `http://localhost:5173` (dev) or `http://localhost:8001` (served from FastAPI).

### Production Deployment

```bash
# Deploy to DigitalOcean droplet
./scripts/deploy.sh botuser@your-server-ip
```

The deploy script rsyncs the project, adjusts ports for production, and rebuilds the container on the remote host. The frontend is pre-built and served as static files from `backend/static/`.

## Trading Modes

The bot supports two modes, switchable from the dashboard sidebar:

- **Paper** (default) -- Simulated fills, no real money. Uses its own bankroll, position sizer, and circuit breaker instance.
- **Live** -- Real orders via Kalshi REST API. Separate bankroll tracking. Requires confirmation and checks for open positions before switching.

A **trading pause** button on the dashboard halts new entries while still allowing open positions to exit normally.

## Backtesting and Strategy Tuning

### Roadmap

The bot's backtesting and tuning capabilities mature as more live data accumulates:

| Milestone | Data Required | What Unlocks |
|-----------|--------------|--------------|
| **Now** | Binance CSV (6 months, included) | Backtest strategy logic against spot BTC data; validate signal generation works |
| **~3 weeks** | 2,000+ live candles | Auto-tuner activates (runs every 6h); walk-forward with 1 window |
| **~2 months** | 8,000+ live candles | Walk-forward with multiple windows; statistically meaningful parameter optimization |
| **~3 months** | 12,000+ live candles + daily attribution history | Signal drift detection; session/regime profitability trends; full attribution time series |

### Running Backtests (Manual)

```bash
cd backend

# Backtest against Binance historical data
python -m backtesting run --csv ../data/candles_btc_15m.csv

# Backtest against live collected data (once enough accumulates)
python -m backtesting run --from-db --symbol BTC --source live_spot,binance

# Walk-forward optimization
python -m backtesting walk-forward --csv ../data/candles_btc_15m.csv

# Manual tuning cycle
python -m backtesting tune --from-db

# Generate HTML report from existing JSON
python -m backtesting report --input backtest_reports/latest.json
```

Results land in `backend/backtest_reports/` and are visible in the dashboard's **Backtest Results** panel. Full interactive HTML reports are accessible via the "Open full HTML report" button.

### Auto-Tuner (Automated)

The coordinator runs a tuning cycle every 6 hours:

1. Loads live candle and order book data from the database
2. Builds a parameter search grid around current settings
3. Runs walk-forward optimization with train/test splits
4. Evaluates whether the recommendation passes safety thresholds
5. Posts results to Discord; does **not** auto-apply by default

Parameter overrides can be viewed and cleared from the dashboard or via the API (`GET/DELETE /api/param-overrides`).

### Performance Attribution (Automated)

- **Daily** (00:05 UTC) -- Queries previous day's trades, runs full PnL attribution, persists to `daily_attribution` table, posts to Discord `#kbtc-attribution`
- **Weekly** (Sunday 00:10 UTC) -- Aggregates the week's daily attribution, detects session/regime drift (profitable-to-unprofitable flips), posts digest to Discord

Attribution breaks down PnL by conviction level, trading session (Asia/London/US), ATR regime, exit reason, and fee drag. Visible in the dashboard's **PnL Attribution** panel.

## Dashboard

The dashboard is a single-page React app at the server's root URL.

| Section | What It Shows |
|---------|--------------|
| **Sidebar** | Bankroll, drawdown, daily/weekly loss, trade count, paper/live toggle, trading pause |
| **Equity / BTC Price** | Equity curve (PnL or account value) and BTC candlestick chart with time range filtering |
| **Signal Panel** | Live OBI %, bid/ask volumes, spread, spot price, ATR regime |
| **System Health** | WebSocket connection status, tick/candle counts, reconnect attempts |
| **Position Table** | Open position, closed trade history (paginated), errored/quarantined trades |
| **Additional Stats** | Best/worst/avg trade, daily PnL bar chart, win rate by regime |
| **PnL Attribution** | Conviction/session/regime breakdown tables, fee drag summary |
| **Backtest Results** | Latest backtest metrics, overfitting warnings, walk-forward recommendation, HTML report link |

## Discord Notifications

Five webhook channels, each independently configurable:

| Channel | Events |
|---------|--------|
| `#kbtc-trades` | Trade opened, trade closed |
| `#kbtc-risk` | Circuit breaker tripped/cleared, ATR regime change, sizing failure |
| `#kbtc-heartbeat` | Periodic heartbeat, 4h/24h performance summaries |
| `#kbtc-errors` | Bot start/stop, WebSocket disconnect, DB errors, quarantined trades |
| `#kbtc-attribution` | Daily attribution report, weekly attribution digest |

## Risk Management

- **Position sizing**: Fixed fractional (2% risk per trade), scaled by conviction (HIGH 1.3x, NORMAL 1.0x, LOW 0.65x) and reduced 50% during drawdowns
- **Circuit breaker**: Halts trading when daily loss exceeds 6%, weekly loss exceeds 15%, or drawdown exceeds 20%
- **ATR regime filter**: Blocks all new entries during HIGH volatility periods
- **Stop loss**: Hard 2% stop on every position
- **Rapid-fire detection**: Quarantines trades if 3+ exits occur within 60 seconds (prevents feedback loops)

## Database

PostgreSQL with TimescaleDB. Key tables:

| Table | Purpose |
|-------|---------|
| `candles` | 15-minute OHLCV from live feeds and Binance |
| `ob_snapshots` | Order book depth snapshots (every 30s) |
| `trades` | All completed trades with full metadata |
| `bankroll_history` | Equity curve snapshots |
| `signal_log` | Every signal evaluation (OBI/ROC/regime/decision) |
| `daily_attribution` | Daily PnL attribution snapshots |
| `param_recommendations` | Auto-tuner recommendations |
| `bot_state` | Key-value store for runtime state (bankroll, param overrides) |

## Tests

```bash
cd backend && python -m pytest tests/ -v
```

Unit tests cover: position sizer, circuit breaker, OBI strategy, ROC strategy, signal resolver, candle aggregator, ATR regime filter, and paper trader.

## Environment Variables

See `.env.example` for the full list. Key variables:

| Variable | Description |
|----------|-------------|
| `KALSHI_API_KEY_ID` | Kalshi API key |
| `KALSHI_PRIVATE_KEY_PATH` | Path to RSA private key for request signing |
| `KALSHI_ENV` | `demo` or `prod` |
| `TRADING_MODE` | `paper` or `live` |
| `INITIAL_BANKROLL` | Starting bankroll in dollars |
| `DISCORD_*_WEBHOOK` | Webhook URLs for trades, risk, heartbeat, errors, attribution channels |
| `TUNING_INTERVAL_HOURS` | How often the auto-tuner runs (default: 6) |
