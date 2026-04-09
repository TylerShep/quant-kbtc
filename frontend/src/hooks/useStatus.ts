import { useEffect, useState, useCallback } from 'react';
import type { StatusResponse } from '../types';

export function useStatus(intervalMs = 5000) {
  const [status, setStatus] = useState<StatusResponse | null>(null);

  const fetchStatus = useCallback(async () => {
    try {
      const res = await fetch('/api/status');
      if (res.ok) {
        const data = await res.json();
        setStatus(data);
      }
    } catch {}
  }, []);

  useEffect(() => {
    fetchStatus();
    const id = setInterval(fetchStatus, intervalMs);
    return () => clearInterval(id);
  }, [fetchStatus, intervalMs]);

  return status;
}
