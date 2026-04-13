import { useState, useEffect, useRef, Component, type ReactNode } from 'react';
import { useWebSocket } from './hooks/useWebSocket';
import { useStatus } from './hooks/useStatus';
import { useTrades } from './hooks/useTrades';
import { useEquity } from './hooks/useEquity';
import { useErroredTrades } from './hooks/useErroredTrades';
import { Sidebar } from './components/Sidebar';
import { PnLChart } from './components/PnLChart';
import { PositionTable } from './components/PositionTable';
import { SignalPanel } from './components/SignalPanel';
import { TopBar } from './components/TopBar';
import { SystemHealth } from './components/SystemHealth';
import { StatsPanel } from './components/StatsPanel';
import { BacktestPanel } from './components/BacktestPanel';
import { AttributionPanel } from './components/AttributionPanel';
import { BtcPriceChart } from './components/BtcPriceChart';
import { useDiagnostics } from './hooks/useDiagnostics';
import type { PnLPoint, Features, MarketState } from './types';
import './index.css';

class ErrorBoundary extends Component<{ children: ReactNode }, { hasError: boolean }> {
  state = { hasError: false };
  static getDerivedStateFromError() { return { hasError: true }; }
  render() {
    if (this.state.hasError) {
      return (
        <div className="flex items-center justify-center h-screen bg-[var(--bg-primary)] text-[var(--text-muted)]">
          <div className="text-center">
            <p className="text-lg mb-2">Dashboard error</p>
            <button
              onClick={() => this.setState({ hasError: false })}
              className="px-4 py-2 bg-[var(--bg-tertiary)] rounded text-sm hover:bg-[var(--border)]"
            >
              Retry
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

export type ChartMode = 'pnl' | 'account';
export type ViewMode = 'paper' | 'live';

function App() {
  const { lastMessage, connected } = useWebSocket();
  const status = useStatus(5000);
  const tradingMode = status?.trading_mode;
  const [viewMode, setViewMode] = useState<ViewMode>('paper');
  const userPickedView = useRef(false);
  const initialSyncDone = useRef(false);

  const handleViewModeChange = (mode: ViewMode) => {
    userPickedView.current = true;
    setViewMode(mode);
  };

  const { equity, stats } = useEquity(viewMode);
  const [tradesPage, setTradesPage] = useState(1);
  const { data: tradesData, loading: tradesLoading } = useTrades(tradesPage, 10, viewMode);
  const [erroredPage, setErroredPage] = useState(1);
  const { data: erroredData, loading: erroredLoading } = useErroredTrades(erroredPage);
  const diagnostics = useDiagnostics(10000);
  const [features, setFeatures] = useState<Features | null>(null);
  const [marketState, setMarketState] = useState<MarketState | null>(null);
  const [timeRange, setTimeRange] = useState<'24H' | '1W' | '1M' | 'All'>('All');
  const [chartMode, setChartMode] = useState<ChartMode>('account');
  const [chartTab, setChartTab] = useState<'equity' | 'btc'>('equity');

  // On first status load, set the default view to match the backend trading mode.
  // After that, only sync when the user hasn't manually picked a tab.
  useEffect(() => {
    if (!tradingMode) return;
    if (!initialSyncDone.current) {
      initialSyncDone.current = true;
      if (tradingMode === 'live' || tradingMode === 'paper') {
        setViewMode(tradingMode as ViewMode);
      }
      return;
    }
    if (!userPickedView.current && (tradingMode === 'live' || tradingMode === 'paper')) {
      setViewMode(tradingMode as ViewMode);
    }
  }, [tradingMode]);

  // Reset trades page when switching viewMode
  useEffect(() => {
    setTradesPage(1);
  }, [viewMode]);

  useEffect(() => {
    if (!lastMessage || lastMessage.type !== 'market_update') return;
    if (lastMessage.data) setFeatures(lastMessage.data);
    if (lastMessage.state) setMarketState(lastMessage.state);
  }, [lastMessage]);

  const initialBankroll = stats?.initial_bankroll ?? 1000;
  const statsReady = stats !== null && equity !== null;

  const allEquityData: PnLPoint[] = equity?.equity?.map((e) => ({
    time: e.time,
    value: chartMode === 'pnl' ? e.bankroll - initialBankroll : e.bankroll,
  })) ?? [];

  const liveEquity = stats?.equity ?? initialBankroll;
  const livePoint: PnLPoint = {
    time: Math.floor(Date.now() / 1000),
    value: chartMode === 'pnl' ? liveEquity - initialBankroll : liveEquity,
  };

  const timeRangeCutoffs: Record<string, number> = {
    '24H': 86400,
    '1W': 604800,
    '1M': 2592000,
  };
  const cutoffSec = timeRangeCutoffs[timeRange];
  const nowSec = Math.floor(Date.now() / 1000);
  const equityData = cutoffSec
    ? allEquityData.filter((p) => p.time >= nowSec - cutoffSec)
    : allEquityData;

  const chartData: PnLPoint[] = statsReady ? [...equityData, livePoint] : [];

  const statusMarket = status?.market_states?.['BTC'];
  const mergedMarket: MarketState | null = marketState ?? (statusMarket ? {
    symbol: 'BTC',
    spot_price: statusMarket.spot_price,
    kalshi_ticker: statusMarket.kalshi_ticker,
    best_bid: statusMarket.best_bid,
    best_ask: statusMarket.best_ask,
    mid: statusMarket.mid,
    spread: statusMarket.spread,
    time_remaining_sec: statusMarket.time_remaining_sec,
    volume: statusMarket.volume,
  } : null);

  const mergedFeatures: Features | null = features ?? (statusMarket ? {
    obi: statusMarket.obi ?? 0.5,
    total_bid_vol: 0,
    total_ask_vol: 0,
    spread_cents: statusMarket.spread,
    spot_price: statusMarket.spot_price,
    mid_price: statusMarket.mid,
  } : null);

  const viewTraderState = viewMode === 'live' ? status?.live : status?.paper;
  const viewRisk = viewMode === 'live' ? (status?.live_risk ?? status?.risk) : (status?.paper_risk ?? status?.risk);

  return (
    <ErrorBoundary>
      <div className="flex flex-col h-screen bg-[var(--bg-primary)]">
        <TopBar
          connected={connected}
          timeRange={timeRange}
          onTimeRangeChange={setTimeRange}
          chartMode={chartMode}
          onChartModeChange={setChartMode}
        />
        <div className="flex flex-1 overflow-hidden">
          <Sidebar
            risk={viewRisk ?? null}
            paper={viewTraderState ?? null}
            features={mergedFeatures}
            stats={stats ?? null}
            tradingMode={tradingMode ?? 'paper'}
            tradingPaused={status?.trading_paused ?? 'off'}
            viewMode={viewMode}
          />
          <main className="flex-1 flex flex-col overflow-hidden">
            {/* View mode tabs */}
            <div className="flex items-center gap-1 px-3 pt-2 border-b border-[var(--border)] pb-2">
              <div className="flex bg-[var(--bg-tertiary)] rounded p-0.5 mr-3">
                <button
                  type="button"
                  onClick={() => handleViewModeChange('paper')}
                  className={`px-3 py-1 text-xs rounded transition-colors font-medium ${
                    viewMode === 'paper'
                      ? 'bg-blue-600 text-white'
                      : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)]'
                  }`}
                >
                  Paper
                </button>
                <button
                  type="button"
                  onClick={() => handleViewModeChange('live')}
                  className={`px-3 py-1 text-xs rounded transition-colors font-medium ${
                    viewMode === 'live'
                      ? 'bg-amber-600 text-white'
                      : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)]'
                  }`}
                >
                  Live
                </button>
              </div>
              <button
                type="button"
                onClick={() => setChartTab('equity')}
                className={`px-3 py-1 text-xs rounded transition-colors ${
                  chartTab === 'equity'
                    ? 'bg-[var(--accent)] text-white'
                    : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)]'
                }`}
              >
                Equity
              </button>
              <button
                type="button"
                onClick={() => setChartTab('btc')}
                className={`px-3 py-1 text-xs rounded transition-colors ${
                  chartTab === 'btc'
                    ? 'bg-[var(--accent)] text-white'
                    : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)]'
                }`}
              >
                BTC Price
              </button>
              {tradingMode === 'live' && viewMode === 'paper' && (
                <span className="ml-auto text-[10px] text-amber-400/70 flex items-center gap-1">
                  <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
                  Live trading active
                </span>
              )}
            </div>
            <div className="flex-1 min-h-0 p-3">
              {chartTab === 'equity' ? (
                <PnLChart data={chartData} mode={chartMode} />
              ) : (
                <BtcPriceChart />
              )}
            </div>
            <SignalPanel
              features={mergedFeatures}
              atr={status?.atr ?? null}
              marketState={mergedMarket}
            />
            <SystemHealth diagnostics={diagnostics} />
            <PositionTable
              paper={viewTraderState ?? null}
              orphanedPositions={viewMode === 'live' ? (status?.orphaned_positions ?? []) : []}
              tradesData={tradesData}
              tradesLoading={tradesLoading}
              onPageChange={setTradesPage}
              erroredData={erroredData}
              erroredLoading={erroredLoading}
              onErroredPageChange={setErroredPage}
            />
            <StatsPanel stats={stats ?? null} tradingMode={viewMode} />
            <AttributionPanel tradingMode={viewMode} />
            <BacktestPanel />
          </main>
        </div>
      </div>
    </ErrorBoundary>
  );
}

export default App;
