import { useMemo } from 'react';
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  LineElement,
  PointElement,
  Tooltip,
  Legend,
  type ChartOptions,
  type ChartData,
} from 'chart.js';
import { Chart } from 'react-chartjs-2';
import type { Candlestick } from '../../hooks/useHistoricals';

ChartJS.register(
  CategoryScale,
  LinearScale,
  BarElement,
  LineElement,
  PointElement,
  Tooltip,
  Legend
);

interface Props {
  candles: Candlestick[];
}

function parseFp(v?: string): number {
  if (v == null) return 0;
  const n = parseFloat(v);
  return Number.isFinite(n) ? n : 0;
}

function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export function VolumeOIChart({ candles }: Props) {
  const { data, options } = useMemo(() => {
    const sorted = [...candles].sort((a, b) => a.end_period_ts - b.end_period_ts);
    const labels = sorted.map((c) => formatTime(c.end_period_ts));
    const volume = sorted.map((c) => parseFp(c.volume_fp));
    const oi = sorted.map((c) => parseFp(c.open_interest_fp));

    const chartData: ChartData<'bar' | 'line'> = {
      labels,
      datasets: [
        {
          type: 'bar' as const,
          label: 'Volume',
          data: volume,
          backgroundColor: 'rgba(127, 119, 221, 0.75)',
          borderColor: '#7F77DD',
          borderWidth: 0,
          borderRadius: 3,
          yAxisID: 'yVolume',
          order: 2,
        },
        {
          type: 'line' as const,
          label: 'Open Interest',
          data: oi,
          borderColor: '#EF9F27',
          backgroundColor: '#EF9F27',
          borderWidth: 2,
          pointRadius: 1.5,
          pointHoverRadius: 3,
          tension: 0.2,
          yAxisID: 'yOI',
          order: 1,
        },
      ],
    };

    const chartOptions: ChartOptions<'bar' | 'line'> = {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          display: true,
          position: 'top',
          align: 'end',
          labels: {
            color: '#c9d1d9',
            font: { size: 10 },
            boxWidth: 14,
            boxHeight: 2,
          },
        },
        tooltip: {
          backgroundColor: '#161b22',
          borderColor: '#30363d',
          borderWidth: 1,
          titleColor: '#c9d1d9',
          bodyColor: '#c9d1d9',
          callbacks: {
            label: (ctx) => {
              const v = ctx.parsed.y;
              if (ctx.dataset.label === 'Volume') return `Volume: ${v?.toLocaleString() ?? '—'}`;
              return `Open Interest: ${v?.toLocaleString() ?? '—'}`;
            },
          },
        },
      },
      scales: {
        x: {
          ticks: { color: '#8b949e', font: { size: 9 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 },
          grid: { color: '#21262d' },
        },
        yVolume: {
          type: 'linear',
          position: 'left',
          ticks: {
            color: '#7F77DD',
            font: { size: 9 },
            callback: (v) => (Number(v) >= 1000 ? `${(Number(v) / 1000).toFixed(1)}k` : String(v)),
          },
          grid: { color: '#21262d' },
          beginAtZero: true,
        },
        yOI: {
          type: 'linear',
          position: 'right',
          ticks: {
            color: '#EF9F27',
            font: { size: 9 },
            callback: (v) => (Number(v) >= 1000 ? `${(Number(v) / 1000).toFixed(1)}k` : String(v)),
          },
          grid: { display: false },
          beginAtZero: true,
        },
      },
    };

    return { data: chartData, options: chartOptions };
  }, [candles]);

  return (
    <div className="w-full h-full p-2">
      <Chart type="bar" data={data as ChartData<'bar'>} options={options as ChartOptions<'bar'>} />
    </div>
  );
}
