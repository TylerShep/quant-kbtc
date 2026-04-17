import { useMemo } from 'react';
import { useHistoricals } from '../hooks/useHistoricals';
import { YesPriceHistory } from './historicals/YesPriceHistory';
import { SpreadBandChart } from './historicals/SpreadBandChart';
import { VolumeOIChart } from './historicals/VolumeOIChart';
import { ResolutionRateChart } from './historicals/ResolutionRateChart';
import { StrikeHeatmap } from './historicals/StrikeHeatmap';

function PanelShell({
  title,
  subtitle,
  children,
  className = '',
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`flex flex-col bg-[var(--bg-secondary)] rounded-lg border border-[var(--border)] overflow-hidden ${className}`}
    >
      <div className="px-3 py-2 border-b border-[var(--border)] flex items-baseline justify-between flex-shrink-0">
        <span className="text-xs font-medium text-[var(--text-primary)]">{title}</span>
        {subtitle && (
          <span className="text-[10px] text-[var(--text-muted)]">{subtitle}</span>
        )}
      </div>
      <div className="flex-1 min-h-0 relative">{children}</div>
    </div>
  );
}

function LoadingOverlay() {
  return (
    <div className="absolute inset-0 flex items-center justify-center text-[var(--text-muted)] text-xs z-10 pointer-events-none">
      Loading…
    </div>
  );
}

function EmptyState({ message }: { message: string }) {
  return (
    <div className="h-full flex items-center justify-center text-[var(--text-muted)] text-xs px-4 text-center">
      {message}
    </div>
  );
}

export function HistoricalsTab() {
  const {
    settled,
    settledSummary,
    currentMarket,
    currentMarketSource,
    activeCandles,
    priceHistoryCandles,
    loading,
    error,
    lastUpdated,
    refresh,
  } = useHistoricals({ priceHistoryLimit: 24, settledSummaryLimit: 2000 });

  const currentSubtitle = useMemo(() => {
    if (!currentMarket) return currentMarketSource === 'none' ? 'no market' : '';
    const label =
      currentMarketSource === 'open' ? 'active' : 'most recent settled';
    const strike =
      currentMarket.floor_strike ?? currentMarket.cap_strike ?? null;
    const strikeStr = strike != null ? `$${Number(strike).toLocaleString()}` : '';
    return `${currentMarket.ticker}${strikeStr ? ` · ${strikeStr}` : ''} · ${label}`;
  }, [currentMarket, currentMarketSource]);

  const updatedLabel = lastUpdated
    ? new Date(lastUpdated).toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      })
    : '—';

  return (
    <div className="w-full h-full flex flex-col gap-2 min-h-0">
      <div className="flex items-center justify-between text-[10px] text-[var(--text-muted)] px-1 flex-shrink-0">
        <span>
          Kalshi KXBTC · {settledSummary.length} settled markets loaded · updated {updatedLabel}
        </span>
        <div className="flex items-center gap-2">
          {error && (
            <span className="text-red-400" title={error}>
              {error.length > 60 ? error.slice(0, 60) + '…' : error}
            </span>
          )}
          <button
            type="button"
            onClick={refresh}
            disabled={loading}
            className="px-2 py-0.5 rounded bg-[var(--bg-tertiary)] hover:bg-[var(--border)] disabled:opacity-50 text-[var(--text-secondary)]"
          >
            Refresh
          </button>
        </div>
      </div>

      <div className="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-2 gap-2">
        <PanelShell
          title="YES Price History"
          subtitle={`last ${settled.length} settled`}
        >
          {loading && settled.length === 0 && <LoadingOverlay />}
          {!loading && priceHistoryCandles.every((p) => !p.candle) ? (
            <EmptyState message="No candlestick data returned for recent settled markets." />
          ) : (
            <YesPriceHistory
              markets={settled}
              priceHistoryCandles={priceHistoryCandles}
            />
          )}
        </PanelShell>

        <PanelShell title="Bid/Ask Spread Band" subtitle={currentSubtitle}>
          {loading && activeCandles.length === 0 && <LoadingOverlay />}
          {!loading && activeCandles.length === 0 ? (
            <EmptyState message="No 1-minute candles available for the current market yet." />
          ) : (
            <SpreadBandChart candles={activeCandles} />
          )}
        </PanelShell>

        <PanelShell title="Volume & Open Interest" subtitle={currentSubtitle}>
          {loading && activeCandles.length === 0 && <LoadingOverlay />}
          {!loading && activeCandles.length === 0 ? (
            <EmptyState message="No volume data available for the current market yet." />
          ) : (
            <VolumeOIChart candles={activeCandles} />
          )}
        </PanelShell>

        <PanelShell
          title="YES Resolution Rate by Hour (EST)"
          subtitle={
            settledSummary.length > 0
              ? `${settledSummary.length} contracts`
              : settled.length > 0
                ? `${settled.length} contracts (loading more…)`
                : ''
          }
        >
          {loading && settled.length === 0 && <LoadingOverlay />}
          {!loading && settled.length === 0 ? (
            <EmptyState message="No settled markets available." />
          ) : (
            <ResolutionRateChart
              markets={settledSummary.length > 0 ? settledSummary : settled}
            />
          )}
        </PanelShell>
      </div>

      <div className="flex-shrink-0">
        <PanelShell
          title="Strike vs. Hour Heatmap"
          subtitle={
            settledSummary.length > 0
              ? `${settledSummary.length} settled · YES resolution rate`
              : settled.length > 0
                ? `${settled.length} settled (loading more…)`
                : ''
          }
          className="h-64"
        >
          {loading && settled.length === 0 && <LoadingOverlay />}
          {!loading && settled.length === 0 ? (
            <EmptyState message="No settled markets available for the heatmap." />
          ) : (
            <StrikeHeatmap
              markets={settledSummary.length > 0 ? settledSummary : settled}
            />
          )}
        </PanelShell>
      </div>
    </div>
  );
}
