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
  type Plugin,
} from 'chart.js';
import { Bar } from 'react-chartjs-2';
import type { SettledMarket } from '../../hooks/useHistoricals';

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
  markets: SettledMarket[];
}

const HOURS = [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20];

function hourLabel(h: number): string {
  if (h === 12) return '12p';
  if (h === 0) return '12a';
  if (h < 12) return `${h}a`;
  return `${h - 12}p`;
}

function estHour(iso: string | null): number | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const estStr = d.toLocaleString('en-US', {
    timeZone: 'America/New_York',
    hour: '2-digit',
    hour12: false,
  });
  const n = parseInt(estStr, 10);
  return Number.isFinite(n) ? n : null;
}

function barColor(rate: number): string {
  if (rate >= 0.6) return '#378ADD';
  if (rate >= 0.55) return '#85B7EB';
  return 'rgba(133, 183, 235, 0.45)';
}

const FIFTY_LINE_PLUGIN: Plugin<'bar'> = {
  id: 'fiftyLine',
  afterDatasetsDraw(chart) {
    const y = chart.scales.y;
    const x = chart.scales.x;
    if (!y || !x) return;
    const yPx = y.getPixelForValue(0.5);
    const ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = '#8b949e';
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x.left, yPx);
    ctx.lineTo(x.right, yPx);
    ctx.stroke();
    ctx.restore();
  },
};

export function ResolutionRateChart({ markets }: Props) {
  const { data, options, stats } = useMemo(() => {
    const buckets: Record<number, { yes: number; total: number }> = {};
    HOURS.forEach((h) => (buckets[h] = { yes: 0, total: 0 }));

    let ungrouped = 0;
    for (const m of markets) {
      if (!m.result) continue;
      const resolutionIso = m.close_time ?? m.expiration_time;
      const h = estHour(resolutionIso);
      if (h == null || buckets[h] == null) {
        ungrouped += 1;
        continue;
      }
      buckets[h].total += 1;
      if (m.result === 'yes') buckets[h].yes += 1;
    }

    const rates = HOURS.map((h) =>
      buckets[h].total > 0 ? buckets[h].yes / buckets[h].total : 0
    );
    const totals = HOURS.map((h) => buckets[h].total);

    const chartData: ChartData<'bar'> = {
      labels: HOURS.map(hourLabel),
      datasets: [
        {
          label: 'YES rate',
          data: rates,
          backgroundColor: rates.map(barColor),
          borderWidth: 0,
          borderRadius: 3,
        },
      ],
    };

    const chartOptions: ChartOptions<'bar'> = {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#161b22',
          borderColor: '#30363d',
          borderWidth: 1,
          titleColor: '#c9d1d9',
          bodyColor: '#c9d1d9',
          callbacks: {
            label: (ctx) => {
              const idx = ctx.dataIndex;
              const h = HOURS[idx];
              const total = buckets[h].total;
              const pct = total > 0 ? ((buckets[h].yes / total) * 100).toFixed(1) : '—';
              return `YES ${pct}% · ${buckets[h].yes}/${total}`;
            },
          },
        },
      },
      scales: {
        x: {
          ticks: { color: '#8b949e', font: { size: 9 } },
          grid: { color: '#21262d' },
        },
        y: {
          min: 0,
          max: 1,
          ticks: {
            color: '#8b949e',
            font: { size: 9 },
            stepSize: 0.1,
            callback: (v) => `${Math.round(Number(v) * 100)}%`,
          },
          grid: { color: '#21262d' },
        },
      },
    };

    return {
      data: chartData,
      options: chartOptions,
      stats: { counted: totals.reduce((a, b) => a + b, 0), ungrouped },
    };
  }, [markets]);

  return (
    <div className="w-full h-full p-2 flex flex-col">
      <div className="flex-1 min-h-0">
        <Bar data={data} options={options} plugins={[FIFTY_LINE_PLUGIN]} />
      </div>
      <div className="text-[9px] text-[var(--text-muted)] text-right px-2 mt-1">
        {stats.counted} contracts grouped · {stats.ungrouped} outside 6a–8p window
      </div>
    </div>
  );
}
