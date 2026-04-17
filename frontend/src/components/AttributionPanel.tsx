import { useEffect, useState } from 'react';

interface ConvictionRow {
  trades: number;
  pnl_dollars: number;
  avg_pnl_pct: number;
  win_rate: number;
  pnl_share_pct: number;
}

interface SessionRow {
  trades: number;
  pnl_dollars: number;
  win_rate: number;
  avg_pnl_pct: number;
}

interface RegimeRow {
  trades: number;
  pnl_dollars: number;
  win_rate: number;
  avg_hold_candles: number;
}

interface ExecutionData {
  total_fees_dollars: number;
  theoretical_pnl: number;
  actual_pnl: number;
  execution_drag: number;
  fees_as_pct_of_gross: number;
}

interface Attribution {
  total_pnl_dollars: number;
  total_trades: number;
  signal_attribution: Record<string, ConvictionRow>;
  regime_attribution: Record<string, RegimeRow>;
  session_attribution: Record<string, SessionRow>;
  execution_attribution: ExecutionData;
}

interface AttrResponse {
  attribution: Attribution;
  mode: string;
}

function fmtUsd(n: number): string {
  const sign = n >= 0 ? '+' : '';
  return `${sign}$${Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtPct(n: number): string {
  return `${n.toFixed(1)}%`;
}

const CONVICTION_ORDER = ['HIGH', 'NORMAL', 'LOW'];
const SESSION_ORDER = ['ASIA', 'LONDON', 'US_OPEN', 'US_MAIN', 'US_CLOSE'];
const REGIME_ORDER = ['LOW', 'MEDIUM', 'HIGH'];

const attrCache: Record<string, { attribution: Attribution; mode: string }> = {};

async function fetchAttrForMode(m: string): Promise<{ attribution: Attribution | null; mode: string }> {
  const modeParam = `?mode=${m}`;
  try {
    const res = await fetch(`/api/attribution${modeParam}`);
    if (res.ok) {
      const j: AttrResponse = await res.json();
      attrCache[m] = { attribution: j.attribution, mode: j.mode };
      return attrCache[m];
    }
  } catch {}
  return attrCache[m] ?? { attribution: null, mode: m };
}

export function AttributionPanel({ tradingMode }: { tradingMode?: string }) {
  const activeMode = tradingMode ?? 'paper';
  const cached = attrCache[activeMode];
  const [expanded, setExpanded] = useState(false);
  const [attr, setAttr] = useState<Attribution | null>(cached?.attribution ?? null);
  const [mode, setMode] = useState<string>(cached?.mode ?? '');
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const c = attrCache[activeMode];
    if (c) {
      setAttr(c.attribution);
      setMode(c.mode);
    }

    let cancelled = false;
    (async () => {
      try {
        const result = await fetchAttrForMode(activeMode);
        if (cancelled) return;
        setAttr(result.attribution);
        setMode(result.mode);
        setErr(null);

        const other = activeMode === 'paper' ? 'live' : 'paper';
        fetchAttrForMode(other);
      } catch {
        if (!cancelled) setErr('Failed to load attribution');
      }
    })();
    return () => { cancelled = true; };
  }, [activeMode]);

  const sig = attr?.signal_attribution ?? {};
  const sess = attr?.session_attribution ?? {};
  const reg = attr?.regime_attribution ?? {};
  const exe = attr?.execution_attribution;

  const convictions = CONVICTION_ORDER.filter((c) => c in sig);
  const sessions = SESSION_ORDER.filter((s) => s in sess);
  const regimes = REGIME_ORDER.filter((r) => r in reg);

  return (
    <div className="border-t border-[var(--border)] bg-[var(--bg-secondary)]">
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        className="w-full flex items-center justify-between px-4 py-2 text-left hover:bg-[var(--bg-tertiary)] transition-colors"
      >
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-[var(--text-primary)]">PnL attribution</span>
          {attr && (
            <span
              className="text-[10px] tabular-nums font-medium"
              style={{ color: attr.total_pnl_dollars >= 0 ? 'var(--green)' : 'var(--red)' }}
            >
              {fmtUsd(attr.total_pnl_dollars)}
            </span>
          )}
        </div>
        <span className="text-[var(--text-muted)] text-xs">{expanded ? '\u25BC' : '\u25B6'}</span>
      </button>

      {expanded && (
        <div className="px-4 pb-4 pt-1 space-y-4 border-t border-[var(--border)]">
          {err && <p className="text-xs text-[var(--red)]">{err}</p>}

          {!attr || attr.total_trades === 0 ? (
            <p className="text-xs text-[var(--text-muted)]">No trade data for attribution yet</p>
          ) : (
            <>
              <p className="text-[10px] text-[var(--text-muted)]">
                {attr.total_trades} trades &middot; {mode} mode
              </p>

              {convictions.length > 0 && (
                <div>
                  <div className="text-xs text-[var(--text-muted)] mb-2">By conviction</div>
                  <AttrTable
                    headers={['Conviction', 'Trades', 'Win Rate', 'PnL', 'Share']}
                    rows={convictions.map((c) => {
                      const s = sig[c] as ConvictionRow;
                      return [c, String(s.trades), fmtPct(s.win_rate * 100), fmtUsd(s.pnl_dollars), fmtPct(s.pnl_share_pct)];
                    })}
                    pnlCol={3}
                  />
                </div>
              )}

              {sessions.length > 0 && (
                <div>
                  <div className="text-xs text-[var(--text-muted)] mb-2">By session</div>
                  <AttrTable
                    headers={['Session', 'Trades', 'Win Rate', 'PnL']}
                    rows={sessions.map((s) => {
                      const d = sess[s] as SessionRow;
                      return [s, String(d.trades), fmtPct(d.win_rate * 100), fmtUsd(d.pnl_dollars)];
                    })}
                    pnlCol={3}
                  />
                </div>
              )}

              {regimes.length > 0 && (
                <div>
                  <div className="text-xs text-[var(--text-muted)] mb-2">By regime</div>
                  <AttrTable
                    headers={['Regime', 'Trades', 'Win Rate', 'Avg Hold', 'PnL']}
                    rows={regimes.map((r) => {
                      const d = reg[r] as RegimeRow;
                      return [r, String(d.trades), fmtPct(d.win_rate * 100), `${d.avg_hold_candles}c`, fmtUsd(d.pnl_dollars)];
                    })}
                    pnlCol={4}
                  />
                </div>
              )}

              {exe && (
                <div className="grid grid-cols-3 gap-3">
                  <div>
                    <div className="text-[10px] uppercase tracking-wide text-[var(--text-muted)]">Total fees</div>
                    <div className="text-sm font-semibold tabular-nums" style={{ color: 'var(--red)' }}>
                      ${Math.abs(exe.total_fees_dollars).toFixed(2)}
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wide text-[var(--text-muted)]">Fee drag</div>
                    <div className="text-sm font-semibold tabular-nums text-[var(--text-primary)]">
                      {fmtPct(exe.fees_as_pct_of_gross)} of gross
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wide text-[var(--text-muted)]">Exec drag</div>
                    <div className="text-sm font-semibold tabular-nums" style={{ color: 'var(--red)' }}>
                      ${Math.abs(exe.execution_drag).toFixed(2)}
                    </div>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function AttrTable({ headers, rows, pnlCol }: { headers: string[]; rows: string[][]; pnlCol: number }) {
  return (
    <div className="rounded border border-[var(--border)] overflow-hidden">
      <table className="w-full text-xs">
        <thead>
          <tr className="bg-[var(--bg-tertiary)] text-[var(--text-muted)]">
            {headers.map((h, i) => (
              <th key={h} className={`font-medium px-2 py-1.5 ${i === 0 ? 'text-left' : 'text-right'}`}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row[0]} className="border-t border-[var(--border)]">
              {row.map((cell, i) => {
                const isPnl = i === pnlCol;
                const numVal = isPnl ? parseFloat(cell.replace(/[^0-9.\-]/g, '')) : 0;
                const isNeg = isPnl && cell.includes('-');
                return (
                  <td
                    key={`${row[0]}-${i}`}
                    className={`px-2 py-1.5 tabular-nums ${i === 0 ? 'text-left text-[var(--text-primary)] font-medium' : 'text-right text-[var(--text-secondary)]'}`}
                    style={isPnl ? { color: isNeg || numVal < 0 ? 'var(--red)' : 'var(--green)', fontWeight: 600 } : undefined}
                  >
                    {cell}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
