import { useEffect, useState, useCallback } from 'react';
import type { TradesResponse } from '../types';

export function useTrades(page: number, perPage = 10) {
  const [data, setData] = useState<TradesResponse | null>(null);
  const [loading, setLoading] = useState(false);

  const fetchTrades = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`/api/trades?page=${page}&per_page=${perPage}`);
      if (res.ok) setData(await res.json());
    } catch {}
    setLoading(false);
  }, [page, perPage]);

  useEffect(() => {
    fetchTrades();
    const id = setInterval(fetchTrades, 15000);
    return () => clearInterval(id);
  }, [fetchTrades]);

  return { data, loading, refetch: fetchTrades };
}
