import { useEffect, useState, useCallback, useRef } from 'react';
import type { TradesResponse } from '../types';

export function useTrades(page: number, perPage = 10, mode: string = 'paper') {
  const [data, setData] = useState<TradesResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const prevMode = useRef(mode);

  const fetchTrades = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`/api/trades?page=${page}&per_page=${perPage}&mode=${mode}`);
      if (res.ok) setData(await res.json());
    } catch {}
    setLoading(false);
  }, [page, perPage, mode]);

  useEffect(() => {
    if (prevMode.current !== mode) {
      setData(null);
      prevMode.current = mode;
    }
    fetchTrades();
    const id = setInterval(fetchTrades, 15000);
    return () => clearInterval(id);
  }, [fetchTrades, mode]);

  return { data, loading, refetch: fetchTrades };
}
