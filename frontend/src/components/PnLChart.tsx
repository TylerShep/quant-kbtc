import { useEffect, useRef } from 'react';
import { createChart, AreaSeries, type IChartApi, type ISeriesApi } from 'lightweight-charts';
import type { PnLPoint } from '../types';

interface PnLChartProps {
  data: PnLPoint[];
}

export function PnLChart({ data }: PnLChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Area'> | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { color: '#0a0e14' },
        textColor: '#5a6577',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: '#1e253020' },
        horzLines: { color: '#1e253040' },
      },
      crosshair: {
        vertLine: { color: '#5a657740', width: 1, style: 3 },
        horzLine: { color: '#5a657740', width: 1, style: 3 },
      },
      rightPriceScale: {
        borderColor: '#1e2530',
        scaleMargins: { top: 0.1, bottom: 0.1 },
      },
      timeScale: {
        borderColor: '#1e2530',
        timeVisible: true,
        secondsVisible: false,
      },
    });

    const series = chart.addSeries(AreaSeries, {
      lineColor: '#f6465d',
      topColor: '#f6465d33',
      bottomColor: '#f6465d05',
      lineWidth: 2,
      crosshairMarkerVisible: true,
      crosshairMarkerRadius: 4,
    });

    chartRef.current = chart;
    seriesRef.current = series;

    const resizeObserver = new ResizeObserver(() => {
      if (containerRef.current) {
        chart.applyOptions({
          width: containerRef.current.clientWidth,
          height: containerRef.current.clientHeight,
        });
      }
    });
    resizeObserver.observe(containerRef.current);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
    };
  }, []);

  useEffect(() => {
    if (!seriesRef.current || data.length === 0) return;

    try {
      const seen = new Set<number>();
      const chartData = data
        .filter((d) => {
          const t = d.time;
          if (seen.has(t)) return false;
          seen.add(t);
          return true;
        })
        .sort((a, b) => a.time - b.time)
        .map((d) => ({ time: d.time as any, value: d.value }));

      if (chartData.length === 0) return;

      const isPositive = chartData[chartData.length - 1].value >= 0;
      seriesRef.current.applyOptions({
        lineColor: isPositive ? '#0ecb81' : '#f6465d',
        topColor: isPositive ? '#0ecb8133' : '#f6465d33',
        bottomColor: isPositive ? '#0ecb8105' : '#f6465d05',
      });

      seriesRef.current.setData(chartData);
    } catch {
      // lightweight-charts can throw on edge-case data; ignore
    }
  }, [data]);

  return (
    <div ref={containerRef} className="w-full h-full rounded-lg overflow-hidden" />
  );
}
