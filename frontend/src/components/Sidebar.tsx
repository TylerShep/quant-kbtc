import type { RiskState, PaperState, Features, CumulativeStats } from '../types';

interface SidebarProps {
  risk: RiskState | null;
  paper: PaperState | null;
  features: Features | null;
  stats: CumulativeStats | null;
}

export function Sidebar({ risk, paper, features, stats }: SidebarProps) {
  const equity = stats?.equity ?? risk?.bankroll ?? 0;
  const drawdown = risk?.drawdown_pct ?? 0;
  const dailyLoss = risk?.daily_loss_pct ?? 0;
  const canTrade = risk?.can_trade ?? false;
  const obi = features?.obi ?? 0.5;
  const bidVol = features?.total_bid_vol ?? 0;
  const askVol = features?.total_ask_vol ?? 0;

  const obiDirection = obi >= 0.65 ? 'LONG' : obi <= 0.35 ? 'SHORT' : 'NEUTRAL';
  const obiColor = obiDirection === 'LONG' ? 'var(--green)' : obiDirection === 'SHORT' ? 'var(--red)' : 'var(--text-muted)';

  const totalPnl = stats?.total_pnl ?? 0;
  const winRate = stats?.win_rate ?? 0;

  return (
    <aside className="w-56 border-r border-[var(--border)] bg-[var(--bg-secondary)] flex flex-col p-3 gap-4 overflow-y-auto">
      <StatBlock label="Equity" value={`$${equity.toLocaleString('en-US', { minimumFractionDigits: 2 })}`} />

      <div>
        <div className="flex justify-between text-xs mb-1">
          <span className="text-[var(--text-muted)]">Direction Bias</span>
          <span style={{ color: obiColor }} className="font-medium">{obiDirection}</span>
        </div>
      </div>

      <div>
        <div className="text-xs text-[var(--text-muted)] mb-1">Position Distribution</div>
        <div className="flex h-5 rounded overflow-hidden text-[10px] font-medium">
          <div
            className="flex items-center justify-center bg-[var(--green)]"
            style={{ width: `${(bidVol / (bidVol + askVol || 1)) * 100}%` }}
          >
            {bidVol > 0 && Math.round((bidVol / (bidVol + askVol || 1)) * 100) + '%'}
          </div>
          <div
            className="flex items-center justify-center bg-[var(--red)]"
            style={{ width: `${(askVol / (bidVol + askVol || 1)) * 100}%` }}
          >
            {askVol > 0 && Math.round((askVol / (bidVol + askVol || 1)) * 100) + '%'}
          </div>
        </div>
      </div>

      <div>
        <div className="text-xs text-[var(--text-muted)] mb-1">Cumulative PnL</div>
        <div
          className="text-lg font-semibold"
          style={{ color: totalPnl >= 0 ? 'var(--green)' : 'var(--red)' }}
        >
          {totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(2)}
        </div>
        <div className="text-xs text-[var(--text-muted)]">
          Win Rate: {(winRate * 100).toFixed(1)}%
        </div>
      </div>

      <hr className="border-[var(--border)]" />

      <StatBlock label="Drawdown" value={`${drawdown.toFixed(2)}%`} color={drawdown > 10 ? 'var(--red)' : 'var(--text-primary)'} />
      <StatBlock label="Daily Loss" value={`${dailyLoss.toFixed(2)}%`} color={dailyLoss > 4 ? 'var(--red)' : 'var(--text-primary)'} />
      <StatBlock label="Trades Today" value={String(risk?.trades_today ?? 0)} />
      <StatBlock label="Total Trades" value={String(stats?.total_trades ?? paper?.total_trades ?? 0)} />

      <div className="mt-auto">
        <div
          className={`text-xs font-medium px-2 py-1 rounded text-center ${
            canTrade
              ? 'bg-[var(--green-dim)] text-[var(--green)]'
              : 'bg-[var(--red-dim)] text-[var(--red)]'
          }`}
        >
          {canTrade ? 'TRADING ACTIVE' : risk?.halt_reason ?? 'HALTED'}
        </div>
      </div>
    </aside>
  );
}

function StatBlock({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color?: string;
}) {
  return (
    <div>
      <div className="text-xs text-[var(--text-muted)]">{label}</div>
      <div className="text-sm font-medium" style={{ color: color ?? 'var(--text-primary)' }}>
        {value}
      </div>
    </div>
  );
}
