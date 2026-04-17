import { useEffect, useRef } from 'react';
import {
  ColorType,
  LineSeries,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts';
import type { Candlestick, SettledMarket } from '../../hooks/useHistoricals';

interface Props {
  markets: SettledMarket[];
  priceHistoryCandles: Array<{ ticker: string; candle: Candlestick | null }>;
}

function parseDollar(v?: string): number | null {
  if (v == null) return null;
  const n = parseFloat(v);
  return Number.isFinite(n) ? n : null;
}

export function YesPriceHistory({ markets, priceHistoryCandles }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const lineRef = useRef<ISeriesApi<'Line'> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: '#0d1117' },
        textColor: '#c9d1d9',
        fontSize: 10,
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
        scaleMargins: { top: 0.1, bottom: 0.1 },
        minimumWidth: 55,
      },
      timeScale: {
        borderColor: '#21262d',
        timeVisible: true,
        secondsVisible: false,
      },
    });

    const line = chart.addSeries(LineSeries, {
      color: '#378ADD',
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      pointMarkersVisible: false,
      priceFormat: { type: 'custom', formatter: (p: number) => `$${p.toFixed(2)}` },
    });
    line.applyOptions({ priceFormat: { type: 'custom', formatter: (p: number) => `$${p.toFixed(2)}`, minMove: 0.01 } });

    const markers = createSeriesMarkers(line, []);

    chartRef.current = chart;
    lineRef.current = line;
    markersRef.current = markers;

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
      lineRef.current = null;
      markersRef.current = null;
    };
  }, []);

  useEffect(() => {
    const line = lineRef.current;
    const markerApi = markersRef.current;
    const chart = chartRef.current;
    if (!line || !markerApi || !chart) return;

    type Pt = {
      time: UTCTimestamp;
      value: number;
      ticker: string;
      result: 'yes' | 'no' | null;
      strike: number | null;
    };

    const points: Pt[] = priceHistoryCandles
      .map(({ ticker, candle }) => {
        if (!candle) return null;
        const price =
          parseDollar(candle.price?.close_dollars) ??
          parseDollar(candle.price?.mean_dollars) ??
          parseDollar(candle.yes_bid?.close_dollars);
        if (price == null) return null;
        const mkt = markets.find((m) => m.ticker === ticker);
        const strike = mkt?.floor_strike ?? mkt?.cap_strike ?? null;
        const t = candle.end_period_ts as UTCTimestamp;
        return {
          time: t,
          value: price,
          ticker,
          result: (mkt?.result ?? null) as Pt['result'],
          strike,
        };
      })
      .filter((p): p is Pt => p !== null)
      .sort((a, b) => (a.time as number) - (b.time as number));

    if (points.length === 0) {
      line.setData([]);
      markerApi.setMarkers([]);
      return;
    }

    const seen = new Set<number>();
    const uniq = points.filter((p) => {
      const t = p.time as number;
      if (seen.has(t)) return false;
      seen.add(t);
      return true;
    });

    line.setData(uniq.map((p) => ({ time: p.time, value: p.value })));

    markerApi.setMarkers(
      uniq.map((p) => ({
        time: p.time,
        position: 'inBar',
        color: p.result === 'yes' ? '#3fb950' : p.result === 'no' ? '#f85149' : '#8b949e',
        shape: 'circle',
        size: 1.2,
        text: '',
      }))
    );

    chart.timeScale().fitContent();
  }, [markets, priceHistoryCandles]);

  return <div ref={containerRef} className="w-full h-full" />;
}
