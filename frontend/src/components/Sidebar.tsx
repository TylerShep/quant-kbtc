import { useState } from 'react';
import type { RiskState, PaperState, Features, CumulativeStats } from '../types';

interface SidebarProps {
  risk: RiskState | null;
  paper: PaperState | null;
  features: Features | null;
  stats: CumulativeStats | null;
  tradingMode?: string;
  tradingPaused?: 'off' | 'settling' | 'paused';
  viewMode?: string;
}

export function Sidebar({ risk, paper, features, stats, tradingMode = 'paper', tradingPaused = 'off', viewMode = 'paper' }: SidebarProps) {
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
      <div>
        <div className="flex items-center gap-1.5 mb-0.5">
          <div className="text-xs text-[var(--text-muted)]">Equity</div>
          <span className={`text-[9px] font-medium px-1 py-0.5 rounded ${
            viewMode === 'live'
              ? 'bg-amber-900/30 text-amber-400'
              : 'bg-blue-900/30 text-blue-400'
          }`}>
            {viewMode === 'live' ? 'LIVE' : 'PAPER'}
          </span>
        </div>
        <div className="text-sm font-medium" style={{ color: 'var(--text-primary)' }}>
          ${equity.toLocaleString('en-US', { minimumFractionDigits: 2 })}
        </div>
      </div>

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

      <div className="mt-auto flex flex-col gap-2">
        <LiveToggle isLive={tradingMode === 'live'} tradingPaused={tradingPaused} />
        <LivePauseToggle
          isLive={tradingMode === 'live'}
          canTrade={canTrade}
          tradingPaused={tradingPaused}
          haltReason={risk?.halt_reason ?? null}
        />
      </div>
    </aside>
  );
}

function LiveToggle({ isLive, tradingPaused }: { isLive: boolean; tradingPaused: string }) {
  const [confirming, setConfirming] = useState(false);
  const [switching, setSwitching] = useState(false);
  const isSettling = tradingPaused === 'settling';

  const handleToggle = async () => {
    const targetMode = isLive ? 'paper' : 'live';

    if (targetMode === 'live' && !confirming) {
      setConfirming(true);
      return;
    }

    setSwitching(true);
    try {
      const res = await fetch('/api/trading-mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: targetMode, confirm: true }),
      });
      const data = await res.json();
      if (!data.success) {
        alert(data.error || 'Failed to switch mode');
      }
    } catch {
      alert('Failed to switch trading mode');
    } finally {
      setSwitching(false);
      setConfirming(false);
    }
  };

  if (isSettling) {
    return (
      <div className="bg-[var(--bg-tertiary)] border border-amber-700/50 rounded p-2 text-xs">
        <div className="text-amber-400 font-medium mb-1 animate-pulse">Settling Live Trades...</div>
        <div className="text-[var(--text-muted)] mb-2">Waiting for open live position to exit naturally before disabling.</div>
        <button
          onClick={async () => {
            try {
              await fetch('/api/trading-pause', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ paused: false }),
              });
            } catch {}
          }}
          className="w-full px-2 py-1 bg-[var(--bg-secondary)] text-[var(--text-secondary)] rounded text-xs hover:bg-[var(--border)]"
        >
          Cancel & Resume
        </button>
      </div>
    );
  }

  if (confirming) {
    return (
      <div className="bg-[var(--bg-tertiary)] border border-[var(--red)] rounded p-2 text-xs">
        <div className="text-[var(--red)] font-medium mb-1">Enable LIVE trading?</div>
        <div className="text-[var(--text-muted)] mb-2">Real funds will be used. Paper trading continues in background.</div>
        <div className="flex gap-2">
          <button
            onClick={handleToggle}
            disabled={switching}
            className="flex-1 px-2 py-1 bg-[var(--red)] text-white rounded text-xs font-medium disabled:opacity-50"
          >
            {switching ? '...' : 'Confirm'}
          </button>
          <button
            onClick={() => setConfirming(false)}
            className="flex-1 px-2 py-1 bg-[var(--bg-secondary)] text-[var(--text-secondary)] rounded text-xs"
          >
            Cancel
          </button>
        </div>
      </div>
    );
  }

  return (
    <button
      onClick={handleToggle}
      disabled={switching}
      className={`w-full text-xs font-medium px-2 py-1.5 rounded text-center transition-colors disabled:opacity-50 ${
        isLive
          ? 'bg-amber-900/30 text-amber-400 border border-amber-700/50'
          : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)] border border-[var(--border)] hover:text-[var(--text-primary)] hover:border-amber-700/50'
      }`}
    >
      {switching ? 'Switching...' : isLive ? 'LIVE TRADING ON' : 'ENABLE LIVE TRADING'}
    </button>
  );
}

function LivePauseToggle({ isLive, canTrade, tradingPaused, haltReason }: {
  isLive: boolean;
  canTrade: boolean;
  tradingPaused: string;
  haltReason: string | null;
}) {
  const [toggling, setToggling] = useState(false);

  const isOff = tradingPaused === 'off';
  const isSettling = tradingPaused === 'settling';
  const isPaused = tradingPaused === 'paused';

  const handleToggle = async () => {
    setToggling(true);
    try {
      const res = await fetch('/api/trading-pause', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paused: isOff }),
      });
      const data = await res.json();
      if (!data.success) {
        alert(data.error || 'Failed to toggle');
      }
    } catch {
      alert('Failed to toggle');
    } finally {
      setToggling(false);
    }
  };

  if (!isLive) {
    return (
      <div className="w-full text-[10px] text-center text-[var(--text-muted)] py-1">
        Paper trading always active
      </div>
    );
  }

  const liveHalted = !canTrade;

  const label = liveHalted
    ? haltReason ?? 'HALTED'
    : isSettling
      ? 'SETTLING...'
      : isPaused
        ? 'LIVE PAUSED'
        : 'LIVE ACTIVE';

  const title = isOff
    ? 'Click to pause live trading'
    : isSettling
      ? 'Waiting for live position to settle'
      : isPaused
        ? 'Click to resume live trading'
        : 'Circuit breaker halted';

  const colorClass = liveHalted
    ? 'bg-[var(--red-dim)] text-[var(--red)]'
    : isSettling
      ? 'bg-amber-900/30 text-amber-400 animate-pulse'
      : isPaused
        ? 'bg-[var(--red-dim)] text-[var(--red)] hover:bg-[var(--red)]/20'
        : 'bg-[var(--green-dim)] text-[var(--green)] hover:bg-[var(--green)]/20';

  return (
    <button
      type="button"
      onClick={handleToggle}
      disabled={toggling || liveHalted}
      className={`w-full text-xs font-medium px-2 py-1.5 rounded text-center transition-colors cursor-pointer disabled:cursor-not-allowed ${colorClass}`}
      title={title}
    >
      {toggling ? 'Switching...' : label}
    </button>
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
