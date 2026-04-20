import { useState } from 'react';
import type { PaperState, DBTrade, TradesResponse, ErroredTrade, ErroredTradesResponse, OrphanedPosition } from '../types';

interface PositionTableProps {
  paper: PaperState | null;
  orphanedPositions: OrphanedPosition[];
  tradesData: TradesResponse | null;
  tradesLoading: boolean;
  onPageChange: (page: number) => void;
  erroredData: ErroredTradesResponse | null;
  erroredLoading: boolean;
  onErroredPageChange: (page: number) => void;
}

type Tab = 'positions' | 'trades' | 'errored';

const TAB_LABELS: Record<Tab, string> = {
  positions: 'Open Position',
  trades: 'Trade History',
  errored: 'Errored Trades',
};

export function PositionTable({
  paper, orphanedPositions, tradesData, tradesLoading, onPageChange,
  erroredData, erroredLoading, onErroredPageChange,
}: PositionTableProps) {
  const [activeTab, setActiveTab] = useState<Tab>('trades');
  const erroredCount = erroredData?.total ?? 0;
  const positionCount = (paper?.position ? 1 : 0) + orphanedPositions.length;

  return (
    <div className="border-t border-[var(--border)] bg-[var(--bg-secondary)]">
      <div className="flex items-center gap-1 px-4 py-1.5 border-b border-[var(--border)]">
        {(['positions', 'trades', 'errored'] as Tab[]).map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-3 py-1 text-xs rounded transition-colors ${
              activeTab === tab
                ? 'bg-[var(--bg-tertiary)] text-[var(--text-primary)]'
                : 'text-[var(--text-muted)] hover:text-[var(--text-secondary)]'
            }`}
          >
            {TAB_LABELS[tab]}
            {tab === 'positions' && positionCount > 0 && (
              <span className="ml-1.5 px-1.5 py-0.5 rounded-full text-[10px] font-medium bg-[var(--accent-dim,rgba(59,130,246,0.15))] text-[var(--accent)]">
                {positionCount}
              </span>
            )}
            {tab === 'errored' && erroredCount > 0 && (
              <span className="ml-1.5 px-1.5 py-0.5 rounded-full text-[10px] font-medium bg-[var(--red-dim)] text-[var(--red)]">
                {erroredCount}
              </span>
            )}
          </button>
        ))}
        <div className="ml-auto text-xs text-[var(--text-muted)]">
          {tradesData?.total ?? paper?.total_trades ?? 0} total trades
        </div>
      </div>

      <div className="overflow-x-auto" style={{ maxHeight: '260px' }}>
        {activeTab === 'positions' ? (
          <PositionsView position={paper?.position ?? null} orphanedPositions={orphanedPositions} />
        ) : activeTab === 'errored' ? (
          <ErroredTradesView
            data={erroredData}
            loading={erroredLoading}
            onPageChange={onErroredPageChange}
          />
        ) : (
          <DBTradesView
            data={tradesData}
            loading={tradesLoading}
            onPageChange={onPageChange}
          />
        )}
      </div>
    </div>
  );
}

function PositionsView({ position, orphanedPositions }: {
  position: PaperState['position'];
  orphanedPositions: OrphanedPosition[];
}) {
  if (!position && orphanedPositions.length === 0) {
    return (
      <div className="text-center py-6 text-xs text-[var(--text-muted)]">
        No open positions
      </div>
    );
  }

  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-[var(--text-muted)]">
          <th className="text-left px-4 py-2 font-normal">Ticker</th>
          <th className="text-left px-4 py-2 font-normal">Status</th>
          <th className="text-left px-4 py-2 font-normal">Type</th>
          <th className="text-right px-4 py-2 font-normal">Contracts</th>
          <th className="text-right px-4 py-2 font-normal">Entry Price</th>
          <th className="text-right px-4 py-2 font-normal">Candles Held</th>
          <th className="text-right px-4 py-2 font-normal">Conviction</th>
          <th className="text-right px-4 py-2 font-normal">Signal</th>
        </tr>
      </thead>
      <tbody>
        {position && (
          <tr className="border-t border-[var(--border)]">
            <td className="px-4 py-2 font-mono text-[var(--accent)]">{position.ticker}</td>
            <td className="px-4 py-2">
              <span className="px-2 py-0.5 rounded text-[10px] font-medium bg-[var(--green-dim)] text-[var(--green)]">
                ACTIVE
              </span>
            </td>
            <td className="px-4 py-2">
              <DirectionBadge direction={position.direction} />
            </td>
            <td className="px-4 py-2 text-right font-mono">{position.contracts}</td>
            <td className="px-4 py-2 text-right font-mono">{position.entry_price}c</td>
            <td className="px-4 py-2 text-right font-mono">{position.candles_held}</td>
            <td className="px-4 py-2 text-right">
              <ConvictionBadge conviction={position.conviction} />
            </td>
            <td className="px-4 py-2 text-right">
              <SignalDriverBadge driver={position.signal_driver} />
            </td>
          </tr>
        )}
        {orphanedPositions.map((o) => (
          <tr key={o.ticker} className="border-t border-[var(--border)] opacity-70">
            <td className="px-4 py-2 font-mono text-[var(--yellow,#eab308)]">{o.ticker}</td>
            <td className="px-4 py-2">
              <span
                className="px-2 py-0.5 rounded text-[10px] font-medium bg-[var(--yellow-dim,rgba(234,179,8,0.15))] text-[var(--yellow,#eab308)]"
                title={o.cause === 'EXPIRY_409' ? 'Settlement reached via 409 Conflict at expiry' : undefined}
              >
                {o.cause === 'EXPIRY_409' ? 'EXPIRY-409' : 'ORPHAN'}
              </span>
            </td>
            <td className="px-4 py-2">
              <DirectionBadge direction={o.direction} />
            </td>
            <td className="px-4 py-2 text-right font-mono">{o.contracts}</td>
            <td className="px-4 py-2 text-right font-mono">{o.avg_entry_price}c</td>
            <td className="px-4 py-2 text-right font-mono text-[var(--text-muted)]">-</td>
            <td className="px-4 py-2 text-right text-[var(--text-muted)] text-[10px]">
              {new Date(o.detected_at).toLocaleTimeString(undefined, {
                hour: '2-digit', minute: '2-digit'
              })}
            </td>
            <td className="px-4 py-2 text-right">
              <SignalDriverBadge driver="-" />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function DBTradesView({
  data,
  loading,
  onPageChange,
}: {
  data: TradesResponse | null;
  loading: boolean;
  onPageChange: (page: number) => void;
}) {
  if (!data || data.trades.length === 0) {
    return (
      <div className="text-center py-6 text-xs text-[var(--text-muted)]">
        {loading ? 'Loading...' : 'No trades yet'}
      </div>
    );
  }

  return (
    <div>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-[var(--text-muted)]">
            <th className="text-left px-3 py-2 font-normal">Ticker</th>
            <th className="text-left px-3 py-2 font-normal">Dir</th>
            <th className="text-right px-3 py-2 font-normal">Contracts</th>
            <th className="text-right px-3 py-2 font-normal">Entry</th>
            <th className="text-right px-3 py-2 font-normal">Exit</th>
            <th className="text-right px-3 py-2 font-normal">PnL</th>
            <th className="text-right px-3 py-2 font-normal">PnL%</th>
            <th className="text-left px-3 py-2 font-normal">Exit Reason</th>
            <th className="text-left px-3 py-2 font-normal">Conviction</th>
            <th className="text-left px-3 py-2 font-normal">Signal</th>
            <th className="text-right px-3 py-2 font-normal">Candles</th>
            <th className="text-right px-3 py-2 font-normal">Time</th>
          </tr>
        </thead>
        <tbody>
          {data.trades.map((trade, i) => (
            <TradeRow key={`${trade.timestamp}-${i}`} trade={trade} />
          ))}
        </tbody>
      </table>

      {data.total_pages > 1 && (
        <Pagination
          page={data.page}
          totalPages={data.total_pages}
          total={data.total}
          onPageChange={onPageChange}
        />
      )}
    </div>
  );
}

function ErroredTradesView({
  data,
  loading,
  onPageChange,
}: {
  data: ErroredTradesResponse | null;
  loading: boolean;
  onPageChange: (page: number) => void;
}) {
  if (!data || data.trades.length === 0) {
    return (
      <div className="text-center py-6 text-xs text-[var(--text-muted)]">
        {loading ? 'Loading...' : 'No errored trades'}
      </div>
    );
  }

  return (
    <div>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-[var(--text-muted)]">
            <th className="text-left px-3 py-2 font-normal">Ticker</th>
            <th className="text-left px-3 py-2 font-normal">Dir</th>
            <th className="text-right px-3 py-2 font-normal">Contracts</th>
            <th className="text-right px-3 py-2 font-normal">PnL</th>
            <th className="text-left px-3 py-2 font-normal">Exit Reason</th>
            <th className="text-left px-3 py-2 font-normal">Error Reason</th>
            <th className="text-left px-3 py-2 font-normal">Signal</th>
            <th className="text-right px-3 py-2 font-normal">Candles</th>
            <th className="text-right px-3 py-2 font-normal">Flagged</th>
          </tr>
        </thead>
        <tbody>
          {data.trades.map((trade, i) => (
            <ErroredTradeRow key={`${trade.timestamp}-${i}`} trade={trade} />
          ))}
        </tbody>
      </table>

      {data.total_pages > 1 && (
        <Pagination
          page={data.page}
          totalPages={data.total_pages}
          total={data.total}
          onPageChange={onPageChange}
        />
      )}
    </div>
  );
}

function ErroredTradeRow({ trade }: { trade: ErroredTrade }) {
  return (
    <tr className="border-t border-[var(--border)] hover:bg-[var(--bg-tertiary)] transition-colors opacity-60">
      <td className="px-3 py-1.5 font-mono text-[var(--text-secondary)] truncate max-w-[120px]">
        {trade.ticker}
      </td>
      <td className="px-3 py-1.5">
        <DirectionBadge direction={trade.direction} />
      </td>
      <td className="px-3 py-1.5 text-right font-mono">{trade.contracts}</td>
      <td
        className="px-3 py-1.5 text-right font-mono"
        style={{ color: 'var(--red)' }}
      >
        ${trade.pnl.toFixed(4)}
      </td>
      <td className="px-3 py-1.5 text-[var(--text-secondary)]">{trade.exit_reason}</td>
      <td className="px-3 py-1.5">
        <span className="px-2 py-0.5 rounded text-[10px] font-medium bg-[var(--red-dim)] text-[var(--red)]">
          {trade.error_reason}
        </span>
      </td>
      <td className="px-3 py-1.5">
        <SignalDriverBadge driver={trade.signal_driver} />
      </td>
      <td className="px-3 py-1.5 text-right font-mono">{trade.candles_held}</td>
      <td className="px-3 py-1.5 text-right text-[var(--text-muted)] font-mono whitespace-nowrap">
        {trade.flagged_at ? new Date(trade.flagged_at).toLocaleString(undefined, {
          month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
        }) : '-'}
      </td>
    </tr>
  );
}

function TradeRow({ trade }: { trade: DBTrade }) {
  return (
    <tr className="border-t border-[var(--border)] hover:bg-[var(--bg-tertiary)] transition-colors">
      <td className="px-3 py-1.5 font-mono text-[var(--accent)] truncate max-w-[120px]">
        {trade.ticker}
      </td>
      <td className="px-3 py-1.5">
        <DirectionBadge direction={trade.direction} />
      </td>
      <td className="px-3 py-1.5 text-right font-mono">{trade.contracts}</td>
      <td className="px-3 py-1.5 text-right font-mono">
        {trade.entry_price != null ? `${trade.entry_price}c` : '-'}
      </td>
      <td className="px-3 py-1.5 text-right font-mono">
        {trade.exit_price != null ? `${trade.exit_price}c` : '-'}
      </td>
      <td
        className="px-3 py-1.5 text-right font-mono"
        style={{ color: trade.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}
      >
        {trade.pnl >= 0 ? '+' : ''}${trade.pnl.toFixed(4)}
      </td>
      <td
        className="px-3 py-1.5 text-right font-mono"
        style={{ color: trade.pnl_pct >= 0 ? 'var(--green)' : 'var(--red)' }}
      >
        {(trade.pnl_pct * 100).toFixed(2)}%
      </td>
      <td className="px-3 py-1.5 text-[var(--text-secondary)]">{trade.exit_reason}</td>
      <td className="px-3 py-1.5">
        <ConvictionBadge conviction={trade.conviction} />
      </td>
      <td className="px-3 py-1.5">
        <SignalDriverBadge driver={trade.signal_driver} />
      </td>
      <td className="px-3 py-1.5 text-right font-mono">{trade.candles_held}</td>
      <td className="px-3 py-1.5 text-right text-[var(--text-muted)] font-mono whitespace-nowrap">
        {trade.closed_at ? new Date(trade.closed_at).toLocaleString(undefined, {
          month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
        }) : '-'}
      </td>
    </tr>
  );
}

function Pagination({
  page,
  totalPages,
  total,
  onPageChange,
}: {
  page: number;
  totalPages: number;
  total: number;
  onPageChange: (p: number) => void;
}) {
  return (
    <div className="flex items-center justify-between px-4 py-2 border-t border-[var(--border)]">
      <span className="text-xs text-[var(--text-muted)]">
        Page {page} of {totalPages} ({total} trades)
      </span>
      <div className="flex gap-1">
        <button
          onClick={() => onPageChange(1)}
          disabled={page <= 1}
          className="px-2 py-1 text-xs rounded bg-[var(--bg-tertiary)] text-[var(--text-secondary)] disabled:opacity-30 hover:bg-[var(--border)] transition-colors"
        >
          First
        </button>
        <button
          onClick={() => onPageChange(page - 1)}
          disabled={page <= 1}
          className="px-2 py-1 text-xs rounded bg-[var(--bg-tertiary)] text-[var(--text-secondary)] disabled:opacity-30 hover:bg-[var(--border)] transition-colors"
        >
          Prev
        </button>
        <button
          onClick={() => onPageChange(page + 1)}
          disabled={page >= totalPages}
          className="px-2 py-1 text-xs rounded bg-[var(--bg-tertiary)] text-[var(--text-secondary)] disabled:opacity-30 hover:bg-[var(--border)] transition-colors"
        >
          Next
        </button>
        <button
          onClick={() => onPageChange(totalPages)}
          disabled={page >= totalPages}
          className="px-2 py-1 text-xs rounded bg-[var(--bg-tertiary)] text-[var(--text-secondary)] disabled:opacity-30 hover:bg-[var(--border)] transition-colors"
        >
          Last
        </button>
      </div>
    </div>
  );
}

function DirectionBadge({ direction }: { direction: string }) {
  return (
    <span
      className={`px-2 py-0.5 rounded text-[10px] font-medium ${
        direction === 'long'
          ? 'bg-[var(--green-dim)] text-[var(--green)]'
          : 'bg-[var(--red-dim)] text-[var(--red)]'
      }`}
    >
      {direction.toUpperCase()}
    </span>
  );
}

function ConvictionBadge({ conviction }: { conviction: string }) {
  return (
    <span
      className={`px-2 py-0.5 rounded text-[10px] font-medium ${
        conviction === 'HIGH'
          ? 'bg-[var(--green-dim)] text-[var(--green)]'
          : conviction === 'LOW'
          ? 'bg-[var(--red-dim)] text-[var(--red)]'
          : 'text-[var(--text-secondary)]'
      }`}
    >
      {conviction}
    </span>
  );
}

function SignalDriverBadge({ driver }: { driver?: string }) {
  const label = driver && driver.length > 0 ? driver : '-';
  if (label === '-') {
    return <span className="text-[10px] text-[var(--text-muted)] font-mono">-</span>;
  }
  const [base, suffix] = label.split('/');
  const baseStyle =
    base === 'OBI+ROC'
      ? 'bg-[var(--green-dim)] text-[var(--green)]'
      : base === 'OBI/ROC'
      ? 'bg-[var(--red-dim)] text-[var(--red)]'
      : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)]';
  const suffixStyle =
    suffix === 'TIGHT'
      ? 'text-[var(--green)]'
      : suffix === 'WIDE'
      ? 'text-[var(--red)]'
      : 'text-[var(--text-muted)]';
  return (
    <span className="inline-flex items-center gap-1 font-mono text-[10px]">
      <span className={`px-1.5 py-0.5 rounded font-medium ${baseStyle}`}>{base}</span>
      {suffix && <span className={suffixStyle}>/{suffix}</span>}
    </span>
  );
}
