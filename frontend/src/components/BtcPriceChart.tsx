import { useCallback, useEffect, useRef } from 'react';
import {
  ColorType,
  createChart,
  CandlestickSeries,
  LineSeries,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts';

interface CandleRow {
  time: number | string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
}

interface BtcPriceResponse {
  candles: CandleRow[];
}

function parseTime(t: number | string): UTCTimestamp {
  if (typeof t === 'number') return t as UTCTimestamp;
  return (Math.floor(new Date(t).getTime() / 1000)) as UTCTimestamp;
}

function rollingSma(closes: number[], period: number): (number | null)[] {
  const out: (number | null)[] = [];
  for (let i = 0; i < closes.length; i++) {
    if (i < period - 1) {
      out.push(null);
      continue;
    }
    let sum = 0;
    for (let j = 0; j < period; j++) sum += closes[i - j];
    out.push(sum / period);
  }
  return out;
}

const POLL_MS = 30_000;

export function BtcPriceChart() {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const sma5Ref = useRef<ISeriesApi<'Line'> | null>(null);
  const sma20Ref = useRef<ISeriesApi<'Line'> | null>(null);
  const didFitRef = useRef(false);

  const applyData = useCallback((candles: CandleRow[]) => {
    if (!candleRef.current || !sma5Ref.current || !sma20Ref.current) return;
    if (candles.length === 0) return;

    const sorted = [...candles]
      .map((c) => ({
        time: parseTime(c.time),
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      }))
      .sort((a, b) => (a.time as number) - (b.time as number));

    const seen = new Set<number>();
    const uniq = sorted.filter((c) => {
      const t = c.time as number;
      if (seen.has(t)) return false;
      seen.add(t);
      return true;
    });

    const closes = uniq.map((c) => c.close);
    const times = uniq.map((c) => c.time);
    const sma5 = rollingSma(closes, 5);
    const sma20 = rollingSma(closes, 20);

    const line5 = sma5
      .map((v, i) => (v == null ? null : { time: times[i], value: v }))
      .filter((p): p is { time: UTCTimestamp; value: number } => p != null);

    const line20 = sma20
      .map((v, i) => (v == null ? null : { time: times[i], value: v }))
      .filter((p): p is { time: UTCTimestamp; value: number } => p != null);

    try {
      candleRef.current.setData(uniq);
      sma5Ref.current.setData(line5);
      sma20Ref.current.setData(line20);
      if (!didFitRef.current && uniq.length > 0) {
        didFitRef.current = true;
        chartRef.current?.timeScale().fitContent();
      }
    } catch {
      // ignore chart data edge cases
    }
  }, []);

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: '#0d1117' },
        textColor: '#c9d1d9',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: '#21262d' },
        horzLines: { color: '#21262d' },
      },
      crosshair: {
        vertLine: { color: '#484f5833', width: 1 },
        horzLine: { color: '#484f5833', width: 1 },
      },
      rightPriceScale: {
        borderColor: '#21262d',
        scaleMargins: { top: 0.08, bottom: 0.15 },
      },
      timeScale: {
        borderColor: '#21262d',
        timeVisible: true,
        secondsVisible: false,
      },
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#3fb950',
      downColor: '#f85149',
      borderVisible: false,
      wickUpColor: '#3fb950',
      wickDownColor: '#f85149',
    });

    const line5 = chart.addSeries(LineSeries, {
      color: '#0ecb81',
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: true,
      title: 'SMA 5',
    });

    const line20 = chart.addSeries(LineSeries, {
      color: '#3b82f6',
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: true,
      title: 'SMA 20',
    });

    chartRef.current = chart;
    candleRef.current = candleSeries;
    sma5Ref.current = line5;
    sma20Ref.current = line20;

    const ro = new ResizeObserver(() => {
      if (containerRef.current) {
        chart.applyOptions({
          width: containerRef.current.clientWidth,
          height: containerRef.current.clientHeight,
        });
      }
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      sma5Ref.current = null;
      sma20Ref.current = null;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      try {
        const res = await fetch('/api/btc-price');
        if (!res.ok || cancelled) return;
        const json: BtcPriceResponse = await res.json();
        if (cancelled || !Array.isArray(json.candles)) return;
        applyData(json.candles);
      } catch {
        /* ignore */
      }
    };

    load();
    const id = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [applyData]);

  return <div ref={containerRef} className="w-full h-full rounded-lg overflow-hidden" />;
}
