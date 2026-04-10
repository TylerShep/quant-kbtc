import { useEffect, useState } from 'react';

interface BacktestResults {
  total_trades: number;
  win_rate: number;
  sharpe_ratio: number;
  sortino_ratio?: number;
  max_drawdown_pct: number;
  recovery_factor?: number;
  breakeven_win_rate?: number;
  total_return_pct?: number;
  total_pnl?: number;
  overfitting_red_flags?: Record<string, boolean>;
}

interface BacktestData {
  available: boolean;
  timestamp?: string;
  results?: BacktestResults;
  trade_count?: number;
  config?: Record<string, unknown>;
  report_file?: string;
}

interface TuningData {
  available: boolean;
  edge_consistency?: number;
  avg_oos_sharpe?: number;
  should_apply?: boolean;
  reason?: string;
  recommended_params?: Record<string, unknown>;
}

function fmtPct(n: number | undefined): string {
  if (n == null) return '--';
  const v = Math.abs(n) <= 1 ? n * 100 : n;
  return `${v.toFixed(1)}%`;
}

function fmtNum(n: number | undefined, decimals = 2): string {
  if (n == null) return '--';
  return n.toFixed(decimals);
}

export function BacktestPanel() {
  const [expanded, setExpanded] = useState(false);
  const [data, setData] = useState<BacktestData | null>(null);
  const [tuning, setTuning] = useState<TuningData | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [bRes, tRes] = await Promise.all([
          fetch('/api/backtest/latest'),
          fetch('/api/backtest/tuning'),
        ]);
        if (cancelled) return;
        if (bRes.ok) setData(await bRes.json());
        if (tRes.ok) setTuning(await tRes.json());
        setErr(null);
      } catch {
        if (!cancelled) setErr('Failed to load backtest data');
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const r = data?.results;
  const hasRedFlags = r?.overfitting_red_flags && Object.values(r.overfitting_red_flags).some(Boolean);

  return (
    <div className="border-t border-[var(--border)] bg-[var(--bg-secondary)]">
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        className="w-full flex items-center justify-between px-4 py-2 text-left hover:bg-[var(--bg-tertiary)] transition-colors"
      >
        <span className="text-xs font-medium text-[var(--text-primary)]">Backtest results</span>
        <span className="text-[var(--text-muted)] text-xs">{expanded ? '\u25BC' : '\u25B6'}</span>
      </button>

      {expanded && (
        <div className="px-4 pb-4 pt-1 space-y-4 border-t border-[var(--border)]">
          {err && <p className="text-xs text-[var(--red)]">{err}</p>}

          {!data?.available ? (
            <p className="text-xs text-[var(--text-muted)]">
              No backtest results yet &mdash; run via CLI:
              <code className="ml-1 px-1.5 py-0.5 rounded bg-[var(--bg-tertiary)] text-[10px]">
                python -m backtesting.cli run --csv data.csv
              </code>
            </p>
          ) : (
            <>
              {data.timestamp && (
                <p className="text-[10px] text-[var(--text-muted)]">
                  Last run: {new Date(data.timestamp).toLocaleString()}
                </p>
              )}

              {hasRedFlags && (
                <div className="rounded bg-[var(--red)]/10 border border-[var(--red)]/30 px-3 py-2">
                  <p className="text-xs font-medium" style={{ color: 'var(--red)' }}>
                    Overfitting warning
                  </p>
                  <ul className="text-[10px] text-[var(--text-secondary)] mt-1 space-y-0.5">
                    {r?.overfitting_red_flags?.sharpe_too_high && <li>Sharpe ratio suspiciously high</li>}
                    {r?.overfitting_red_flags?.win_rate_too_high && <li>Win rate unrealistically high</li>}
                    {r?.overfitting_red_flags?.too_few_trades && <li>Too few trades for significance</li>}
                  </ul>
                </div>
              )}

              <div className="grid grid-cols-4 gap-3">
                <MetricCell label="Total trades" value={String(r?.total_trades ?? '--')} />
                <MetricCell label="Win rate" value={fmtPct(r?.win_rate)} good={(r?.win_rate ?? 0) > 0.5} />
                <MetricCell label="Sharpe" value={fmtNum(r?.sharpe_ratio)} good={(r?.sharpe_ratio ?? 0) > 0} />
                <MetricCell label="Sortino" value={fmtNum(r?.sortino_ratio)} good={(r?.sortino_ratio ?? 0) > 0} />
                <MetricCell label="Max DD" value={fmtPct(r?.max_drawdown_pct)} bad={(r?.max_drawdown_pct ?? 0) > 0.15} />
                <MetricCell label="Recovery" value={fmtNum(r?.recovery_factor)} good={(r?.recovery_factor ?? 0) > 2} />
                <MetricCell label="Break-even WR" value={fmtPct(r?.breakeven_win_rate)} />
                <MetricCell label="Return" value={r?.total_return_pct != null ? `${r.total_return_pct.toFixed(1)}%` : '--'} good={(r?.total_return_pct ?? 0) > 0} />
              </div>

              {tuning?.available && (
                <div className="rounded border border-[var(--border)] px-3 py-2 space-y-1">
                  <p className="text-[10px] uppercase tracking-wide text-[var(--text-muted)]">Walk-forward recommendation</p>
                  <p className="text-xs text-[var(--text-primary)]">{tuning.reason ?? '--'}</p>
                  <div className="flex gap-4 text-[10px] text-[var(--text-secondary)]">
                    <span>Consistency: {tuning.edge_consistency != null ? fmtPct(tuning.edge_consistency) : '--'}</span>
                    <span>OOS Sharpe: {fmtNum(tuning.avg_oos_sharpe)}</span>
                    <span>Apply: {tuning.should_apply ? 'Yes' : 'No'}</span>
                  </div>
                </div>
              )}

              {data.report_file && (
                <a
                  href={`/api/backtest/report/${data.report_file}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-block px-3 py-1.5 text-xs rounded bg-[var(--accent)] text-white hover:opacity-90 transition-opacity"
                >
                  Open full HTML report
                </a>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function MetricCell({ label, value, good, bad }: { label: string; value: string; good?: boolean; bad?: boolean }) {
  let color = 'var(--text-primary)';
  if (good) color = 'var(--green)';
  if (bad) color = 'var(--red)';
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-[var(--text-muted)]">{label}</div>
      <div className="text-sm font-semibold tabular-nums" style={{ color }}>{value}</div>
    </div>
  );
}
