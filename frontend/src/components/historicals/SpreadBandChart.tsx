import { useMemo } from 'react';
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  LineElement,
  PointElement,
  Tooltip,
  Legend,
  Filler,
  type ChartOptions,
  type ChartData,
} from 'chart.js';
import { Line } from 'react-chartjs-2';
import type { Candlestick } from '../../hooks/useHistoricals';

ChartJS.register(
  CategoryScale,
  LinearScale,
  LineElement,
  PointElement,
  Tooltip,
  Legend,
  Filler
);

interface Props {
  candles: Candlestick[];
}

function parseDollar(v?: string): number | null {
  if (v == null) return null;
  const n = parseFloat(v);
  return Number.isFinite(n) ? n : null;
}

function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export function SpreadBandChart({ candles }: Props) {
  const { data, options } = useMemo(() => {
    const sorted = [...candles].sort((a, b) => a.end_period_ts - b.end_period_ts);
    const labels = sorted.map((c) => formatTime(c.end_period_ts));
    const ask = sorted.map((c) => parseDollar(c.yes_ask?.close_dollars));
    const bid = sorted.map((c) => parseDollar(c.yes_bid?.close_dollars));

    const chartData: ChartData<'line'> = {
      labels,
      datasets: [
        {
          label: 'Yes Ask',
          data: ask as number[],
          borderColor: '#E24B4A',
          borderDash: [4, 2],
          borderWidth: 1.5,
          pointRadius: 0,
          pointHoverRadius: 3,
          fill: '+1',
          backgroundColor: 'rgba(226, 75, 74, 0.14)',
          tension: 0.15,
          spanGaps: true,
        },
        {
          label: 'Yes Bid',
          data: bid as number[],
          borderColor: '#5DCAA5',
          borderWidth: 1.5,
          pointRadius: 0,
          pointHoverRadius: 3,
          fill: false,
          tension: 0.15,
          spanGaps: true,
        },
      ],
    };

    const chartOptions: ChartOptions<'line'> = {
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
              return `${ctx.dataset.label}: $${v?.toFixed(3) ?? '—'}`;
            },
            afterBody: (items) => {
              if (items.length < 2) return '';
              const askV = items.find((i) => i.dataset.label === 'Yes Ask')?.parsed.y;
              const bidV = items.find((i) => i.dataset.label === 'Yes Bid')?.parsed.y;
              if (askV == null || bidV == null) return '';
              const spread = askV - bidV;
              return `Spread: $${spread.toFixed(3)} (${(spread * 100).toFixed(1)}¢)`;
            },
          },
        },
      },
      scales: {
        x: {
          ticks: { color: '#8b949e', font: { size: 9 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 },
          grid: { color: '#21262d' },
        },
        y: {
          min: 0,
          max: 1,
          ticks: {
            color: '#8b949e',
            font: { size: 9 },
            callback: (v) => `$${Number(v).toFixed(2)}`,
            stepSize: 0.1,
          },
          grid: { color: '#21262d' },
        },
      },
    };

    return { data: chartData, options: chartOptions };
  }, [candles]);

  return (
    <div className="w-full h-full p-2">
      <Line data={data} options={options} />
    </div>
  );
}
