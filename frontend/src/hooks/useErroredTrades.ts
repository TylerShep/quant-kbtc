import { useEffect, useState, useRef } from 'react';
import type { ErroredTradesResponse } from '../types';

const cache: Record<string, ErroredTradesResponse> = {};

function cacheKey(page: number, perPage: number, mode: string): string {
  return `${mode}:${page}:${perPage}`;
}

export function useErroredTrades(page: number, perPage = 10, mode: string = 'paper') {
  const key = cacheKey(page, perPage, mode);
  const [data, setData] = useState<ErroredTradesResponse | null>(cache[key] ?? null);
  const [loading, setLoading] = useState(false);
  const activeModeRef = useRef(mode);

  useEffect(() => {
    activeModeRef.current = mode;

    const cached = cache[key];
    if (cached) setData(cached);

    let cancelled = false;

    async function fetchActive() {
      setLoading(true);
      try {
        const res = await fetch(`/api/errored-trades?page=${page}&per_page=${perPage}&mode=${mode}`);
        if (res.ok) {
          const json = await res.json();
          cache[cacheKey(page, perPage, mode)] = json;
          if (!cancelled && activeModeRef.current === mode) {
            setData(json);
          }
        }
      } catch {}
      if (!cancelled && activeModeRef.current === mode) {
        setLoading(false);
      }

      const other = mode === 'paper' ? 'live' : 'paper';
      const otherKey = cacheKey(1, perPage, other);
      if (!cache[otherKey]) {
        try {
          const res = await fetch(`/api/errored-trades?page=1&per_page=${perPage}&mode=${other}`);
          if (res.ok) cache[otherKey] = await res.json();
        } catch {}
      }
    }

    fetchActive();
    const id = setInterval(fetchActive, 30000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [page, perPage, mode, key]);

  return { data, loading };
}
