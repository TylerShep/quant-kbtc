import { useState } from 'react';
import type { DiagnosticsResponse } from '../types';

interface SystemHealthProps {
  diagnostics: DiagnosticsResponse | null;
}

function StatusDot({ ok }: { ok: boolean }) {
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full ${ok ? 'bg-[var(--green)]' : 'bg-[var(--red)]'}`}
    />
  );
}

export function SystemHealth({ diagnostics }: SystemHealthProps) {
  const [expanded, setExpanded] = useState(false);

  if (!diagnostics) return null;

  const { kalshi_ws, spot_ws } = diagnostics;
  const allHealthy = kalshi_ws.connected && spot_ws.connected && diagnostics.tick_count > 0;

  return (
    <div className="border-t border-[var(--border)] bg-[var(--bg-secondary)]">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-4 py-1.5 text-xs hover:bg-[var(--bg-tertiary)] transition-colors"
      >
        <div className="flex items-center gap-2">
          <StatusDot ok={allHealthy} />
          <span className="text-[var(--text-muted)]">System Health</span>
          {!allHealthy && (
            <span className="text-[var(--red)] font-medium">
              {!kalshi_ws.connected && !spot_ws.connected
                ? 'Both feeds down'
                : !kalshi_ws.connected
                ? 'Kalshi feed down'
                : !spot_ws.connected
                ? 'Spot feed down'
                : 'No ticks'}
            </span>
          )}
        </div>
        <span className="text-[var(--text-muted)]">{expanded ? '▲' : '▼'}</span>
      </button>

      {expanded && (
        <div className="px-4 pb-3 grid grid-cols-2 gap-x-6 gap-y-2 text-xs">
          <div>
            <div className="text-[var(--text-muted)] mb-1 font-medium">Kalshi WebSocket</div>
            <div className="flex items-center gap-2">
              <StatusDot ok={kalshi_ws.connected} />
              <span>{kalshi_ws.connected ? 'Connected' : 'Disconnected'}</span>
            </div>
            <div className="text-[var(--text-muted)] mt-1">
              Messages: {kalshi_ws.message_count.toLocaleString()}
            </div>
            <div className="text-[var(--text-muted)]">
              Last msg: {kalshi_ws.last_message_age_sec != null ? `${kalshi_ws.last_message_age_sec}s ago` : 'never'}
            </div>
            <div className="text-[var(--text-muted)]">
              Reconnects: {kalshi_ws.connect_attempts}
            </div>
            <div className="text-[var(--text-muted)]">
              Tickers: {Object.values(kalshi_ws.active_tickers).join(', ') || 'none'}
            </div>
          </div>

          <div>
            <div className="text-[var(--text-muted)] mb-1 font-medium">Coinbase Spot</div>
            <div className="flex items-center gap-2">
              <StatusDot ok={spot_ws.connected} />
              <span>{spot_ws.connected ? 'Connected' : 'Disconnected'}</span>
            </div>
            <div className="text-[var(--text-muted)] mt-1">
              Messages: {spot_ws.message_count.toLocaleString()}
            </div>
            <div className="text-[var(--text-muted)]">
              Last msg: {spot_ws.last_message_age_sec != null ? `${spot_ws.last_message_age_sec}s ago` : 'never'}
            </div>
            <div className="text-[var(--text-muted)]">
              Reconnects: {spot_ws.connect_attempts}
            </div>
          </div>

          <div className="col-span-2 border-t border-[var(--border)] pt-2 mt-1">
            <div className="flex gap-6">
              <div>
                <span className="text-[var(--text-muted)]">Ticks: </span>
                <span className="font-mono">{diagnostics.tick_count.toLocaleString()}</span>
              </div>
              <div>
                <span className="text-[var(--text-muted)]">Candles: </span>
                <span className="font-mono">{diagnostics.candle_count}</span>
              </div>
              <div>
                <span className="text-[var(--text-muted)]">ATR: </span>
                <span className="font-mono">{diagnostics.atr_regime}</span>
              </div>
              <div>
                <span className="text-[var(--text-muted)]">Mode: </span>
                <span className="font-mono">{diagnostics.trading_mode}</span>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
