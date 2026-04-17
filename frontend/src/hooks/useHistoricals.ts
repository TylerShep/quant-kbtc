import { useCallback, useEffect, useRef, useState } from 'react';

export interface SettledMarket {
  ticker: string;
  event_ticker: string | null;
  result: 'yes' | 'no' | null;
  floor_strike: number | null;
  cap_strike: number | null;
  strike_type: string | null;
  open_time: string | null;
  close_time: string | null;
  expiration_time: string | null;
  volume: string | number | null;
  volume_24h: string | number | null;
  open_interest: string | number | null;
  settlement_value_dollars: string | null;
  last_price_dollars: string | null;
}

export interface Candlestick {
  end_period_ts: number;
  open_interest_fp: string;
  volume_fp: string;
  price?: {
    open_dollars?: string;
    high_dollars?: string;
    low_dollars?: string;
    close_dollars?: string;
    mean_dollars?: string;
    previous_dollars?: string;
  };
  yes_bid?: {
    open_dollars?: string;
    high_dollars?: string;
    low_dollars?: string;
    close_dollars?: string;
  };
  yes_ask?: {
    open_dollars?: string;
    high_dollars?: string;
    low_dollars?: string;
    close_dollars?: string;
  };
}

export interface CandleResponse {
  ticker: string;
  period_interval: number;
  source: 'live' | 'historical';
  candlesticks: Candlestick[];
}

export interface CurrentMarketResponse {
  market: {
    ticker: string;
    event_ticker: string | null;
    result: string | null;
    floor_strike: number | null;
    cap_strike: number | null;
    strike_type: string | null;
    open_time: string | null;
    close_time: string | null;
    expiration_time: string | null;
    status: string | null;
    yes_bid_dollars: string | null;
    yes_ask_dollars: string | null;
  } | null;
  source: 'open' | 'most_recent_settled' | 'none';
}

export interface HistoricalsData {
  settled: SettledMarket[];
  settledSummary: SettledMarket[];
  currentMarket: CurrentMarketResponse['market'] | null;
  currentMarketSource: CurrentMarketResponse['source'];
  activeCandles: Candlestick[];
  priceHistoryCandles: Array<{ ticker: string; candle: Candlestick | null }>;
  loading: boolean;
  error: string | null;
  lastUpdated: number | null;
}

const SPREAD_POLL_MS = 30_000;

const initialState: HistoricalsData = {
  settled: [],
  settledSummary: [],
  currentMarket: null,
  currentMarketSource: 'none',
  activeCandles: [],
  priceHistoryCandles: [],
  loading: true,
  error: null,
  lastUpdated: null,
};

function toUnix(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.floor(t / 1000);
}

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}: ${url}`);
  }
  return (await res.json()) as T;
}

export function useHistoricals(opts: {
  priceHistoryLimit?: number;
  settledSummaryLimit?: number;
} = {}) {
  const { priceHistoryLimit = 24, settledSummaryLimit = 200 } = opts;
  const [state, setState] = useState<HistoricalsData>(initialState);
  const cancelledRef = useRef(false);

  const loadAll = useCallback(async () => {
    cancelledRef.current = false;
    setState((s) => ({ ...s, loading: true, error: null }));

    try {
      const [settledTop, current] = await Promise.all([
        fetchJson<{ markets: SettledMarket[] }>(
          `/api/historicals/settled-markets?limit=${priceHistoryLimit}`
        ),
        fetchJson<CurrentMarketResponse>('/api/historicals/current-market'),
      ]);

      if (cancelledRef.current) return;

      const market = current.market;
      let activeCandles: Candlestick[] = [];
      if (market?.ticker) {
        const openUnix = toUnix(market.open_time) ?? Math.floor(Date.now() / 1000) - 3600;
        const closeUnix =
          toUnix(market.expiration_time) ??
          toUnix(market.close_time) ??
          Math.floor(Date.now() / 1000);
        const endUnix = Math.min(closeUnix, Math.floor(Date.now() / 1000));
        try {
          const resp = await fetchJson<CandleResponse>(
            `/api/historicals/candlesticks?ticker=${encodeURIComponent(market.ticker)}&period_interval=1&start_ts=${openUnix}&end_ts=${endUnix}`
          );
          activeCandles = resp.candlesticks ?? [];
        } catch {
          activeCandles = [];
        }
      }
      if (cancelledRef.current) return;

      const priceHistory = await Promise.all(
        settledTop.markets.map(async (m) => {
          const openUnix = toUnix(m.open_time);
          const endUnix =
            toUnix(m.expiration_time) ??
            toUnix(m.close_time) ??
            Math.floor(Date.now() / 1000);
          if (!openUnix || !endUnix || endUnix <= openUnix) {
            return { ticker: m.ticker, candle: null };
          }
          try {
            const resp = await fetchJson<CandleResponse>(
              `/api/historicals/candlesticks?ticker=${encodeURIComponent(m.ticker)}&period_interval=60&start_ts=${openUnix}&end_ts=${endUnix}`
            );
            const cs = resp.candlesticks ?? [];
            const last = cs.length > 0 ? cs[cs.length - 1] : null;
            return { ticker: m.ticker, candle: last };
          } catch {
            return { ticker: m.ticker, candle: null };
          }
        })
      );

      if (cancelledRef.current) return;

      setState((s) => ({
        ...s,
        settled: settledTop.markets,
        currentMarket: market,
        currentMarketSource: current.source,
        activeCandles,
        priceHistoryCandles: priceHistory,
        loading: false,
        error: null,
        lastUpdated: Date.now(),
      }));

      try {
        const settledBulk = await fetchJson<{ markets: SettledMarket[] }>(
          `/api/historicals/settled-markets?limit=${settledSummaryLimit}`
        );
        if (cancelledRef.current) return;
        setState((s) => ({
          ...s,
          settledSummary: settledBulk.markets,
          lastUpdated: Date.now(),
        }));
      } catch {
        // Panels 4/5 will fall back to `settled` (panel 1's data) if summary fetch fails.
      }
    } catch (e) {
      if (cancelledRef.current) return;
      setState((s) => ({
        ...s,
        loading: false,
        error: e instanceof Error ? e.message : String(e),
      }));
    }
  }, [priceHistoryLimit, settledSummaryLimit]);

  const pollActiveCandles = useCallback(async () => {
    const market = stateRef.current.currentMarket;
    if (!market?.ticker) return;
    if (stateRef.current.currentMarketSource !== 'open') return;

    const openUnix = toUnix(market.open_time) ?? Math.floor(Date.now() / 1000) - 3600;
    const closeUnix =
      toUnix(market.expiration_time) ??
      toUnix(market.close_time) ??
      Math.floor(Date.now() / 1000);
    const endUnix = Math.min(closeUnix, Math.floor(Date.now() / 1000));

    try {
      const resp = await fetchJson<CandleResponse>(
        `/api/historicals/candlesticks?ticker=${encodeURIComponent(market.ticker)}&period_interval=1&start_ts=${openUnix}&end_ts=${endUnix}`
      );
      if (cancelledRef.current) return;
      setState((s) => ({
        ...s,
        activeCandles: resp.candlesticks ?? s.activeCandles,
        lastUpdated: Date.now(),
      }));
    } catch {
      // ignore poll errors
    }
  }, []);

  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    loadAll();
    return () => {
      cancelledRef.current = true;
    };
  }, [loadAll]);

  useEffect(() => {
    const id = setInterval(pollActiveCandles, SPREAD_POLL_MS);
    return () => clearInterval(id);
  }, [pollActiveCandles]);

  return { ...state, refresh: loadAll };
}
