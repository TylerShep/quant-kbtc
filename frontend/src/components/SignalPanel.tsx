import type { Features, ATRState, MarketState } from '../types';

interface SignalPanelProps {
  features: Features | null;
  atr: ATRState | null;
  marketState: MarketState | null;
}

export function SignalPanel({ features, atr, marketState }: SignalPanelProps) {
  const obi = features?.obi ?? 0.5;
  const regime = atr?.regime ?? 'MEDIUM';
  const timeRemaining = marketState?.time_remaining_sec ?? null;

  const obiPct = (obi * 100).toFixed(1);
  const obiDirection = obi >= 0.65 ? 'LONG' : obi <= 0.35 ? 'SHORT' : 'NEUTRAL';

  const regimeColor =
    regime === 'HIGH' ? 'var(--red)' : regime === 'LOW' ? 'var(--text-muted)' : 'var(--green)';

  const formatTime = (sec: number | null) => {
    if (sec === null) return '--:--';
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${m}:${s.toString().padStart(2, '0')}`;
  };

  return (
    <div className="flex items-center gap-6 px-4 py-2 border-t border-[var(--border)] bg-[var(--bg-secondary)] text-xs">
      <div className="flex items-center gap-2">
        <span className="text-[var(--text-muted)]">OBI</span>
        <span
          className="font-mono font-medium"
          style={{
            color:
              obiDirection === 'LONG'
                ? 'var(--green)'
                : obiDirection === 'SHORT'
                ? 'var(--red)'
                : 'var(--text-secondary)',
          }}
        >
          {obiPct}%
        </span>
        <div className="w-20 h-1.5 rounded-full bg-[var(--bg-primary)] overflow-hidden">
          <div
            className="h-full rounded-full transition-all duration-300"
            style={{
              width: `${obi * 100}%`,
              backgroundColor:
                obiDirection === 'LONG'
                  ? 'var(--green)'
                  : obiDirection === 'SHORT'
                  ? 'var(--red)'
                  : 'var(--text-muted)',
            }}
          />
        </div>
      </div>

      <div className="flex items-center gap-2">
        <span className="text-[var(--text-muted)]">ATR Regime</span>
        <span className="font-medium" style={{ color: regimeColor }}>
          {regime}
        </span>
        {atr?.atr_pct != null && (
          <span className="text-[var(--text-muted)]">({atr.atr_pct.toFixed(3)}%)</span>
        )}
      </div>

      <div className="flex items-center gap-2">
        <span className="text-[var(--text-muted)]">Spread</span>
        <span className="font-mono">{features?.spread_cents ?? '--'}c</span>
      </div>

      <div className="flex items-center gap-2">
        <span className="text-[var(--text-muted)]">BTC Spot</span>
        <span className="font-mono">
          ${features?.spot_price?.toLocaleString('en-US', { minimumFractionDigits: 2 }) ?? '--'}
        </span>
      </div>

      <div className="ml-auto flex items-center gap-2">
        <span className="text-[var(--text-muted)]">Expiry</span>
        <span
          className="font-mono font-medium"
          style={{
            color:
              timeRemaining !== null && timeRemaining < 180
                ? 'var(--red)'
                : 'var(--text-primary)',
          }}
        >
          {formatTime(timeRemaining)}
        </span>
      </div>

      <div className="flex items-center gap-2">
        <span className="text-[var(--text-muted)]">Ticker</span>
        <span className="font-mono text-[var(--accent)]">
          {marketState?.kalshi_ticker ?? '--'}
        </span>
      </div>
    </div>
  );
}
