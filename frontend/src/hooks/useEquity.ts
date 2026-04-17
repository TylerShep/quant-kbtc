import { useEffect, useState, useRef } from 'react';
import type { EquityResponse, CumulativeStats } from '../types';

type ModeCache = { equity: EquityResponse | null; stats: CumulativeStats | null };

const cache: Record<string, ModeCache> = {
  paper: { equity: null, stats: null },
  live: { equity: null, stats: null },
};

async function fetchModeData(mode: string): Promise<ModeCache> {
  const modeParam = `?mode=${mode}`;
  try {
    const [eqRes, stRes] = await Promise.all([
      fetch(`/api/equity${modeParam}`),
      fetch(`/api/stats${modeParam}`),
    ]);
    const equity = eqRes.ok ? await eqRes.json() : cache[mode]?.equity ?? null;
    const stats = stRes.ok ? await stRes.json() : cache[mode]?.stats ?? null;
    cache[mode] = { equity, stats };
    return cache[mode];
  } catch {
    return cache[mode] ?? { equity: null, stats: null };
  }
}

export function useEquity(mode: string = 'paper') {
  const [equity, setEquity] = useState<EquityResponse | null>(cache[mode]?.equity ?? null);
  const [stats, setStats] = useState<CumulativeStats | null>(cache[mode]?.stats ?? null);
  const activeModeRef = useRef(mode);

  useEffect(() => {
    activeModeRef.current = mode;

    const cached = cache[mode];
    if (cached?.equity) setEquity(cached.equity);
    if (cached?.stats) setStats(cached.stats);

    let cancelled = false;

    async function fetchActive() {
      const result = await fetchModeData(mode);
      if (cancelled || activeModeRef.current !== mode) return;
      setEquity(result.equity);
      setStats(result.stats);

      const other = mode === 'paper' ? 'live' : 'paper';
      fetchModeData(other);
    }

    fetchActive();
    const id = setInterval(fetchActive, 15000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [mode]);

  return { equity, stats };
}
