import { useState, useEffect } from 'react';
import { useWebSocket } from './hooks/useWebSocket';
import { useStatus } from './hooks/useStatus';
import { Sidebar } from './components/Sidebar';
import { PnLChart } from './components/PnLChart';
import { PositionTable } from './components/PositionTable';
import { SignalPanel } from './components/SignalPanel';
import { TopBar } from './components/TopBar';
import type { PnLPoint, Features, MarketState } from './types';
import './index.css';

function App() {
  const { lastMessage, connected } = useWebSocket();
  const status = useStatus(5000);
  const [features, setFeatures] = useState<Features | null>(null);
  const [marketState, setMarketState] = useState<MarketState | null>(null);
  const [pnlHistory, setPnlHistory] = useState<PnLPoint[]>([]);
  const [timeRange, setTimeRange] = useState<'24H' | '1W' | '1M' | 'All'>('24H');

  useEffect(() => {
    if (lastMessage?.type === 'market_update') {
      setFeatures(lastMessage.data);
      setMarketState(lastMessage.state);

      if (status?.risk?.bankroll) {
        setPnlHistory((prev) => {
          const next = [
            ...prev,
            {
              time: Date.now(),
              value: status.risk.bankroll - (status.risk.peak_bankroll ?? status.risk.bankroll),
            },
          ];
          return next.slice(-2000);
        });
      }
    }
  }, [lastMessage, status?.risk?.bankroll, status?.risk?.peak_bankroll]);

  return (
    <div className="flex flex-col h-screen bg-[var(--bg-primary)]">
      <TopBar
        connected={connected}
        timeRange={timeRange}
        onTimeRangeChange={setTimeRange}
      />
      <div className="flex flex-1 overflow-hidden">
        <Sidebar
          risk={status?.risk ?? null}
          paper={status?.paper ?? null}
          features={features}
        />
        <main className="flex-1 flex flex-col overflow-hidden">
          <div className="flex-1 min-h-0 p-3">
            <PnLChart data={pnlHistory} />
          </div>
          <SignalPanel
            features={features}
            atr={status?.atr ?? null}
            marketState={marketState}
          />
          <PositionTable paper={status?.paper ?? null} />
        </main>
      </div>
    </div>
  );
}

export default App;
