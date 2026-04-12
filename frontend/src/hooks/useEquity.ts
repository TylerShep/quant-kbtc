import { useEffect, useState, useCallback } from 'react';
import type { EquityResponse, CumulativeStats } from '../types';

export function useEquity(mode: string = 'paper') {
  const [equity, setEquity] = useState<EquityResponse | null>(null);
  const [stats, setStats] = useState<CumulativeStats | null>(null);

  const fetchAll = useCallback(async () => {
    const modeParam = `?mode=${mode}`;
    try {
      const [eqRes, stRes] = await Promise.all([
        fetch(`/api/equity${modeParam}`),
        fetch(`/api/stats${modeParam}`),
      ]);
      if (eqRes.ok) setEquity(await eqRes.json());
      if (stRes.ok) setStats(await stRes.json());
    } catch {}
  }, [mode]);

  useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, 30000);
    return () => clearInterval(id);
  }, [fetchAll]);

  return { equity, stats, refetch: fetchAll };
}
