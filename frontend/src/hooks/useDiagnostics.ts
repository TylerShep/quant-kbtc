import { useEffect, useState, useCallback } from 'react';
import type { DiagnosticsResponse } from '../types';

export function useDiagnostics(intervalMs = 10000) {
  const [diagnostics, setDiagnostics] = useState<DiagnosticsResponse | null>(null);

  const fetchDiagnostics = useCallback(async () => {
    try {
      const res = await fetch('/api/diagnostics');
      if (res.ok) {
        setDiagnostics(await res.json());
      }
    } catch {}
  }, []);

  useEffect(() => {
    fetchDiagnostics();
    const id = setInterval(fetchDiagnostics, intervalMs);
    return () => clearInterval(id);
  }, [fetchDiagnostics, intervalMs]);

  return diagnostics;
}
