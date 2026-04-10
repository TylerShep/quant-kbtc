import { useEffect, useState } from 'react';
import type { CumulativeStats } from '../types';

interface StatsPanelProps {
  stats: CumulativeStats | null;
}

interface DailyStatRow {
  date: string;
  trades: number;
  pnl: number;
  wins: number;
  losses: number;
  win_rate: number;
}

interface DailyStatsResponse {
  daily: DailyStatRow[];
}

interface RegimeStatRow {
  regime: string;
  trades: number;
  pnl: number;
  wins: number;
  win_rate: number;
}

interface RegimeStatsResponse {
  regimes: RegimeStatRow[];
}

function formatUsd(n: number): string {
  const sign = n >= 0 ? '+' : '';
  return `${sign}$${Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatPct(rate: number): string {
  const pct = rate <= 1 && rate >= 0 ? rate * 100 : rate;
  return `${pct.toFixed(1)}%`;
}

export function StatsPanel({ stats }: StatsPanelProps) {
  const [expanded, setExpanded] = useState(false);
  const [daily, setDaily] = useState<DailyStatRow[]>([]);
  const [regimes, setRegimes] = useState<RegimeStatRow[]>([]);
  const [loadErr, setLoadErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [dRes, rRes] = await Promise.all([
          fetch('/api/stats/daily'),
          fetch('/api/stats/by-regime'),
        ]);
        if (cancelled) return;
        if (dRes.ok) {
          const j: DailyStatsResponse = await dRes.json();
          setDaily(Array.isArray(j.daily) ? j.daily : []);
        }
        if (rRes.ok) {
          const j: RegimeStatsResponse = await rRes.json();
          setRegimes(Array.isArray(j.regimes) ? j.regimes : []);
        }
        setLoadErr(null);
      } catch {
        if (!cancelled) setLoadErr('Failed to load stats');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const best = stats?.best_trade;
  const worst = stats?.worst_trade;
  const avg = stats?.avg_pnl;

  const dailySorted = [...daily].sort((a, b) => a.date.localeCompare(b.date));
  const dailyWindow = dailySorted.slice(-21);
  const maxAbsPnl =
    dailyWindow.length === 0 ? 1 : Math.max(...dailyWindow.map((d) => Math.abs(d.pnl)), 1e-6);

  return (
    <div className="border-t border-[var(--border)] bg-[var(--bg-secondary)]">
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        className="w-full flex items-center justify-between px-4 py-2 text-left hover:bg-[var(--bg-tertiary)] transition-colors"
      >
        <span className="text-xs font-medium text-[var(--text-primary)]">Additional stats</span>
        <span className="text-[var(--text-muted)] text-xs">{expanded ? '▼' : '▶'}</span>
      </button>

      {expanded && (
        <div className="px-4 pb-4 pt-1 space-y-4 border-t border-[var(--border)]">
          {loadErr && (
            <p className="text-xs text-[var(--red)]">{loadErr}</p>
          )}

          <div className="grid grid-cols-3 gap-3">
            <div>
              <div className="text-[10px] uppercase tracking-wide text-[var(--text-muted)]">Best trade</div>
              <div
                className="text-sm font-semibold tabular-nums"
                style={{ color: best != null && best >= 0 ? 'var(--green)' : 'var(--text-primary)' }}
              >
                {best != null ? formatUsd(best) : '—'}
              </div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wide text-[var(--text-muted)]">Worst trade</div>
              <div
                className="text-sm font-semibold tabular-nums"
                style={{ color: worst != null && worst < 0 ? 'var(--red)' : 'var(--text-primary)' }}
              >
                {worst != null ? formatUsd(worst) : '—'}
              </div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wide text-[var(--text-muted)]">Avg PnL</div>
              <div
                className="text-sm font-semibold tabular-nums"
                style={{ color: avg != null && avg >= 0 ? 'var(--green)' : avg != null ? 'var(--red)' : 'var(--text-primary)' }}
              >
                {avg != null ? formatUsd(avg) : '—'}
              </div>
            </div>
          </div>

          <div>
            <div className="text-xs text-[var(--text-muted)] mb-2">Daily PnL</div>
            {dailyWindow.length === 0 ? (
              <p className="text-xs text-[var(--text-muted)]">No daily history yet</p>
            ) : (
              <div className="flex items-end gap-0.5 h-20">
                {dailyWindow.map((d) => {
                  const h = Math.max(4, (Math.abs(d.pnl) / maxAbsPnl) * 100);
                  const pos = d.pnl >= 0;
                  return (
                    <div
                      key={d.date}
                      className="flex-1 min-w-0 flex flex-col items-center justify-end group"
                      title={`${d.date}: ${formatUsd(d.pnl)} (${d.trades} trades)`}
                    >
                      <div
                        className="w-full max-w-[10px] mx-auto rounded-sm transition-opacity group-hover:opacity-90"
                        style={{
                          height: `${h}%`,
                          backgroundColor: pos ? 'var(--green)' : 'var(--red)',
                          opacity: 0.85,
                        }}
                      />
                    </div>
                  );
                })}
              </div>
            )}
            {dailyWindow.length > 0 && (
              <div className="flex justify-between mt-1 text-[10px] text-[var(--text-muted)]">
                <span>{dailyWindow[0]?.date}</span>
                <span>{dailyWindow[dailyWindow.length - 1]?.date}</span>
              </div>
            )}
          </div>

          <div>
            <div className="text-xs text-[var(--text-muted)] mb-2">Win rate by regime</div>
            {regimes.length === 0 ? (
              <p className="text-xs text-[var(--text-muted)]">No regime breakdown yet</p>
            ) : (
              <div className="rounded border border-[var(--border)] overflow-hidden">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="bg-[var(--bg-tertiary)] text-[var(--text-muted)]">
                      <th className="text-left font-medium px-2 py-1.5">Regime</th>
                      <th className="text-right font-medium px-2 py-1.5">Trades</th>
                      <th className="text-right font-medium px-2 py-1.5">Win rate</th>
                      <th className="text-right font-medium px-2 py-1.5">PnL</th>
                    </tr>
                  </thead>
                  <tbody>
                    {regimes.map((r) => (
                      <tr key={r.regime} className="border-t border-[var(--border)]">
                        <td className="px-2 py-1.5 text-[var(--text-primary)] font-medium">{r.regime}</td>
                        <td className="px-2 py-1.5 text-right tabular-nums text-[var(--text-secondary)]">{r.trades}</td>
                        <td className="px-2 py-1.5 text-right tabular-nums text-[var(--text-secondary)]">
                          {formatPct(r.win_rate)}
                        </td>
                        <td
                          className="px-2 py-1.5 text-right tabular-nums font-medium"
                          style={{ color: r.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}
                        >
                          {formatUsd(r.pnl)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
