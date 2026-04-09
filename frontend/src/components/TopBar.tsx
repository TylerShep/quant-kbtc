import type { ChartMode } from '../App';

interface TopBarProps {
  connected: boolean;
  timeRange: string;
  onTimeRangeChange: (range: '24H' | '1W' | '1M' | 'All') => void;
  chartMode: ChartMode;
  onChartModeChange: (mode: ChartMode) => void;
}

const ranges: Array<'24H' | '1W' | '1M' | 'All'> = ['24H', '1W', '1M', 'All'];

const chartModes: Array<{ key: ChartMode; label: string }> = [
  { key: 'pnl', label: 'PnL' },
  { key: 'account', label: 'Account Value' },
];

export function TopBar({ connected, timeRange, onTimeRangeChange, chartMode, onChartModeChange }: TopBarProps) {
  return (
    <header className="flex items-center justify-between px-4 py-2 border-b border-[var(--border)] bg-[var(--bg-secondary)]">
      <div className="flex items-center gap-4">
        <h1 className="text-base font-semibold tracking-tight text-[var(--text-primary)]">
          KBTC
        </h1>
        <span className="text-xs text-[var(--text-muted)]">Kalshi BTC 15m</span>
        <span
          className={`inline-block w-2 h-2 rounded-full ${
            connected ? 'bg-[var(--green)]' : 'bg-[var(--red)]'
          }`}
          title={connected ? 'Connected' : 'Disconnected'}
        />
      </div>

      <div className="flex items-center gap-1">
        {ranges.map((r) => (
          <button
            key={r}
            onClick={() => onTimeRangeChange(r)}
            className={`px-3 py-1 text-xs rounded transition-colors ${
              timeRange === r
                ? 'bg-[var(--accent)] text-white'
                : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)]'
            }`}
          >
            {r}
          </button>
        ))}
      </div>

      <div className="flex items-center gap-1">
        {chartModes.map(({ key, label }) => (
          <button
            key={key}
            onClick={() => onChartModeChange(key)}
            className={`px-3 py-1 text-xs rounded transition-colors ${
              chartMode === key
                ? 'bg-[var(--bg-tertiary)] text-[var(--text-primary)] ring-1 ring-[var(--border)]'
                : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)]'
            }`}
          >
            {label}
          </button>
        ))}
      </div>
    </header>
  );
}
