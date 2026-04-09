interface TopBarProps {
  connected: boolean;
  timeRange: string;
  onTimeRangeChange: (range: '24H' | '1W' | '1M' | 'All') => void;
}

const ranges: Array<'24H' | '1W' | '1M' | 'All'> = ['24H', '1W', '1M', 'All'];

export function TopBar({ connected, timeRange, onTimeRangeChange }: TopBarProps) {
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

      <div className="flex items-center gap-2">
        {['PnL', 'Account Value'].map((label) => (
          <button
            key={label}
            className="px-3 py-1 text-xs rounded text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] transition-colors"
          >
            {label}
          </button>
        ))}
      </div>
    </header>
  );
}
