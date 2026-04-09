import { useState } from 'react';
import type { PaperState } from '../types';

interface PositionTableProps {
  paper: PaperState | null;
}

type Tab = 'positions' | 'trades';

export function PositionTable({ paper }: PositionTableProps) {
  const [activeTab, setActiveTab] = useState<Tab>('positions');

  return (
    <div className="border-t border-[var(--border)] bg-[var(--bg-secondary)]">
      <div className="flex items-center gap-1 px-4 py-1.5 border-b border-[var(--border)]">
        {(['positions', 'trades'] as Tab[]).map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-3 py-1 text-xs rounded transition-colors capitalize ${
              activeTab === tab
                ? 'bg-[var(--bg-tertiary)] text-[var(--text-primary)]'
                : 'text-[var(--text-muted)] hover:text-[var(--text-secondary)]'
            }`}
          >
            {tab === 'positions' ? 'Asset Positions' : 'Recent Trades'}
          </button>
        ))}
        <div className="ml-auto text-xs text-[var(--text-muted)]">
          {paper?.total_trades ?? 0} total trades
        </div>
      </div>

      <div className="overflow-x-auto max-h-48">
        {activeTab === 'positions' ? (
          <PositionsView position={paper?.position ?? null} />
        ) : (
          <TradesView trades={paper?.recent_trades ?? []} />
        )}
      </div>
    </div>
  );
}

function PositionsView({ position }: { position: PaperState['position'] }) {
  if (!position) {
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
          <th className="text-left px-4 py-2 font-normal">Asset</th>
          <th className="text-left px-4 py-2 font-normal">Type</th>
          <th className="text-right px-4 py-2 font-normal">Contracts</th>
          <th className="text-right px-4 py-2 font-normal">Entry Price</th>
          <th className="text-right px-4 py-2 font-normal">Candles Held</th>
          <th className="text-right px-4 py-2 font-normal">Conviction</th>
        </tr>
      </thead>
      <tbody>
        <tr className="border-t border-[var(--border)]">
          <td className="px-4 py-2 font-medium">BTC</td>
          <td className="px-4 py-2">
            <span
              className={`px-2 py-0.5 rounded text-[10px] font-medium ${
                position.direction === 'long'
                  ? 'bg-[var(--green-dim)] text-[var(--green)]'
                  : 'bg-[var(--red-dim)] text-[var(--red)]'
              }`}
            >
              {position.direction.toUpperCase()}
            </span>
          </td>
          <td className="px-4 py-2 text-right font-mono">{position.contracts}</td>
          <td className="px-4 py-2 text-right font-mono">{position.entry_price}c</td>
          <td className="px-4 py-2 text-right font-mono">{position.candles_held}</td>
          <td className="px-4 py-2 text-right">
            <span
              className={`px-2 py-0.5 rounded text-[10px] font-medium ${
                position.conviction === 'HIGH'
                  ? 'bg-[var(--green-dim)] text-[var(--green)]'
                  : position.conviction === 'LOW'
                  ? 'bg-[var(--red-dim)] text-[var(--red)]'
                  : 'text-[var(--text-secondary)]'
              }`}
            >
              {position.conviction}
            </span>
          </td>
        </tr>
      </tbody>
    </table>
  );
}

function TradesView({ trades }: { trades: PaperState['recent_trades'] }) {
  if (trades.length === 0) {
    return (
      <div className="text-center py-6 text-xs text-[var(--text-muted)]">
        No trades yet
      </div>
    );
  }

  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-[var(--text-muted)]">
          <th className="text-left px-4 py-2 font-normal">Ticker</th>
          <th className="text-left px-4 py-2 font-normal">Direction</th>
          <th className="text-right px-4 py-2 font-normal">PnL</th>
          <th className="text-left px-4 py-2 font-normal">Exit Reason</th>
          <th className="text-right px-4 py-2 font-normal">Time</th>
        </tr>
      </thead>
      <tbody>
        {trades.map((trade, i) => (
          <tr key={i} className="border-t border-[var(--border)]">
            <td className="px-4 py-2 font-mono text-[var(--accent)]">{trade.ticker}</td>
            <td className="px-4 py-2">
              <span
                className={`px-2 py-0.5 rounded text-[10px] font-medium ${
                  trade.direction === 'long'
                    ? 'bg-[var(--green-dim)] text-[var(--green)]'
                    : 'bg-[var(--red-dim)] text-[var(--red)]'
                }`}
              >
                {trade.direction.toUpperCase()}
              </span>
            </td>
            <td
              className="px-4 py-2 text-right font-mono"
              style={{ color: trade.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}
            >
              {trade.pnl >= 0 ? '+' : ''}${trade.pnl.toFixed(2)}
            </td>
            <td className="px-4 py-2 text-[var(--text-secondary)]">{trade.exit_reason}</td>
            <td className="px-4 py-2 text-right text-[var(--text-muted)] font-mono">
              {new Date(trade.exit_time).toLocaleTimeString()}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
