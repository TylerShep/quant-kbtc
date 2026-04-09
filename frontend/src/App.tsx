import { useState, useEffect, Component, type ReactNode } from 'react';
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

function App() {
  const { lastMessage, connected } = useWebSocket();
  const status = useStatus(5000);
  const { equity, stats } = useEquity();
  const [tradesPage, setTradesPage] = useState(1);
  const { data: tradesData, loading: tradesLoading } = useTrades(tradesPage);
  const [erroredPage, setErroredPage] = useState(1);
  const { data: erroredData, loading: erroredLoading } = useErroredTrades(erroredPage);
  const [features, setFeatures] = useState<Features | null>(null);
  const [marketState, setMarketState] = useState<MarketState | null>(null);
  const [timeRange, setTimeRange] = useState<'24H' | '1W' | '1M' | 'All'>('All');
  const [chartMode, setChartMode] = useState<ChartMode>('account');

  useEffect(() => {
    if (!lastMessage || lastMessage.type !== 'market_update') return;
    if (lastMessage.data) setFeatures(lastMessage.data);
    if (lastMessage.state) setMarketState(lastMessage.state);
  }, [lastMessage]);

  const initialBankroll = stats?.initial_bankroll ?? 1000;

  const equityData: PnLPoint[] = equity?.equity?.map((e) => ({
    time: e.time,
    value: chartMode === 'pnl' ? e.bankroll - initialBankroll : e.bankroll,
  })) ?? [];

  const liveEquity = stats?.equity ?? status?.risk?.bankroll ?? initialBankroll;
  const livePoint: PnLPoint | null = {
    time: Math.floor(Date.now() / 1000),
    value: chartMode === 'pnl' ? liveEquity - initialBankroll : liveEquity,
  };

  const chartData: PnLPoint[] = [...equityData, livePoint];

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
            risk={status?.risk ?? null}
            paper={status?.paper ?? null}
            features={mergedFeatures}
            stats={stats ?? null}
          />
          <main className="flex-1 flex flex-col overflow-hidden">
            <div className="flex-1 min-h-0 p-3">
              <PnLChart data={chartData} mode={chartMode} />
            </div>
            <SignalPanel
              features={mergedFeatures}
              atr={status?.atr ?? null}
              marketState={mergedMarket}
            />
            <PositionTable
              paper={status?.paper ?? null}
              tradesData={tradesData}
              tradesLoading={tradesLoading}
              onPageChange={setTradesPage}
              erroredData={erroredData}
              erroredLoading={erroredLoading}
              onErroredPageChange={setErroredPage}
            />
          </main>
        </div>
      </div>
    </ErrorBoundary>
  );
}

export default App;
