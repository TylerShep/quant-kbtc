import { useEffect, useMemo, useRef, useState } from 'react';
import type { SettledMarket } from '../../hooks/useHistoricals';

interface Props {
  markets: SettledMarket[];
}

const HOURS = [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20];

const BUCKETS: Array<{ key: string; label: string; min: number; max: number }> = [
  { key: 'deep_above', label: 'Deep above', min: 2000, max: Infinity },
  { key: 'above', label: 'Above', min: 500, max: 2000 },
  { key: 'near_plus', label: 'Near (+)', min: 0, max: 500 },
  { key: 'near_minus', label: 'Near (−)', min: -500, max: 0 },
  { key: 'below', label: 'Below', min: -2000, max: -500 },
  { key: 'deep_below', label: 'Deep below', min: -Infinity, max: -2000 },
];

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

function pickStrike(m: SettledMarket): number | null {
  if (m.floor_strike != null) return Number(m.floor_strike);
  if (m.cap_strike != null) return Number(m.cap_strike);
  return null;
}

/**
 * Bucket each market relative to the median strike of markets sharing its
 * event_ticker. This approximates "spot at contract open" without needing a
 * separate BTC price-history lookup per contract.
 */
function bucketKey(offset: number): string {
  for (const b of BUCKETS) {
    if (offset > b.min && offset <= b.max) return b.key;
  }
  return BUCKETS[BUCKETS.length - 1].key;
}

function rateToColor(rate: number | null): string {
  if (rate == null) return 'rgba(48, 54, 61, 0.45)';
  const t = Math.max(0, Math.min(1, rate));
  const hue = 5 + t * 135;
  const sat = 60 + t * 15;
  const light = 28 + t * 18;
  return `hsl(${hue}, ${sat}%, ${light}%)`;
}

interface Cell {
  hour: number;
  bucket: string;
  yes: number;
  total: number;
  rate: number | null;
}

export function StrikeHeatmap({ markets }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });
  const [hover, setHover] = useState<Cell | null>(null);

  const { cells, totalContracts } = useMemo(() => {
    const byEvent: Record<string, number[]> = {};
    for (const m of markets) {
      const ev = m.event_ticker ?? m.ticker?.split('-').slice(0, 2).join('-') ?? '';
      const s = pickStrike(m);
      if (!s) continue;
      (byEvent[ev] ??= []).push(s);
    }

    const medianByEvent: Record<string, number> = {};
    for (const [ev, strikes] of Object.entries(byEvent)) {
      const sorted = [...strikes].sort((a, b) => a - b);
      const mid = Math.floor(sorted.length / 2);
      medianByEvent[ev] =
        sorted.length % 2 === 0
          ? (sorted[mid - 1] + sorted[mid]) / 2
          : sorted[mid];
    }

    const grid: Record<string, Cell> = {};
    for (const h of HOURS) {
      for (const b of BUCKETS) {
        grid[`${h}:${b.key}`] = { hour: h, bucket: b.key, yes: 0, total: 0, rate: null };
      }
    }

    let total = 0;
    for (const m of markets) {
      if (!m.result) continue;
      const ev = m.event_ticker ?? m.ticker?.split('-').slice(0, 2).join('-') ?? '';
      const median = medianByEvent[ev];
      const s = pickStrike(m);
      if (median == null || s == null) continue;
      const offset = s - median;
      const bKey = bucketKey(offset);
      const resolutionIso = m.close_time ?? m.expiration_time;
      const h = estHour(resolutionIso);
      if (h == null) continue;
      const key = `${h}:${bKey}`;
      if (!grid[key]) continue;
      grid[key].total += 1;
      if (m.result === 'yes') grid[key].yes += 1;
      total += 1;
    }

    for (const c of Object.values(grid)) {
      c.rate = c.total > 0 ? c.yes / c.total : null;
    }

    return { cells: grid, totalContracts: total };
  }, [markets]);

  useEffect(() => {
    if (!wrapRef.current) return;
    const ro = new ResizeObserver(() => {
      const rect = wrapRef.current!.getBoundingClientRect();
      setSize({ w: Math.floor(rect.width), h: Math.floor(rect.height) });
    });
    ro.observe(wrapRef.current);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || size.w === 0 || size.h === 0) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = size.w * dpr;
    canvas.height = size.h * dpr;
    canvas.style.width = `${size.w}px`;
    canvas.style.height = `${size.h}px`;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size.w, size.h);

    const padLeft = 78;
    const padBottom = 22;
    const padTop = 6;
    const padRight = 8;
    const plotW = size.w - padLeft - padRight;
    const plotH = size.h - padBottom - padTop;
    const cellW = plotW / HOURS.length;
    const cellH = plotH / BUCKETS.length;

    BUCKETS.forEach((b, row) => {
      HOURS.forEach((h, col) => {
        const cell = cells[`${h}:${b.key}`];
        const x = padLeft + col * cellW;
        const y = padTop + row * cellH;
        ctx.fillStyle = rateToColor(cell?.rate ?? null);
        ctx.fillRect(x + 0.5, y + 0.5, cellW - 1, cellH - 1);
        if (cell && cell.total > 0) {
          ctx.fillStyle = cell.rate != null && cell.rate > 0.55 ? '#0d1117' : '#c9d1d9';
          ctx.font = `${Math.min(11, Math.floor(cellH / 3))}px sans-serif`;
          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          const pct = Math.round((cell.rate ?? 0) * 100);
          ctx.fillText(`${pct}%`, x + cellW / 2, y + cellH / 2);
        }
      });
    });

    ctx.fillStyle = '#8b949e';
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    BUCKETS.forEach((b, row) => {
      ctx.fillText(b.label, padLeft - 6, padTop + row * cellH + cellH / 2);
    });
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    HOURS.forEach((h, col) => {
      ctx.fillText(hourLabel(h), padLeft + col * cellW + cellW / 2, padTop + plotH + 4);
    });
  }, [size, cells]);

  const handleMove = (e: React.MouseEvent<HTMLDivElement>) => {
    const rect = wrapRef.current!.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const padLeft = 78;
    const padTop = 6;
    const padRight = 8;
    const padBottom = 22;
    const plotW = rect.width - padLeft - padRight;
    const plotH = rect.height - padBottom - padTop;
    const cellW = plotW / HOURS.length;
    const cellH = plotH / BUCKETS.length;
    const col = Math.floor((x - padLeft) / cellW);
    const row = Math.floor((y - padTop) / cellH);
    if (col < 0 || col >= HOURS.length || row < 0 || row >= BUCKETS.length) {
      setHover(null);
      return;
    }
    const h = HOURS[col];
    const b = BUCKETS[row];
    setHover(cells[`${h}:${b.key}`] ?? null);
  };

  const hoverLabel = hover
    ? (() => {
        const b = BUCKETS.find((x) => x.key === hover.bucket);
        const rate =
          hover.total > 0 ? `${((hover.rate ?? 0) * 100).toFixed(1)}%` : '—';
        return `${b?.label ?? ''} @ ${hourLabel(hover.hour)}  ·  ${rate}  ·  ${hover.yes}/${hover.total}`;
      })()
    : `${totalContracts} contracts bucketed · strike relative to event median`;

  return (
    <div className="w-full h-full flex flex-col p-2">
      <div className="text-[10px] text-[var(--text-muted)] mb-1 flex items-center justify-between">
        <span>{hoverLabel}</span>
        <Legend />
      </div>
      <div
        ref={wrapRef}
        className="flex-1 relative"
        onMouseMove={handleMove}
        onMouseLeave={() => setHover(null)}
      >
        <canvas ref={canvasRef} className="absolute inset-0" />
      </div>
    </div>
  );
}

function Legend() {
  const stops = [0, 0.25, 0.5, 0.75, 1];
  return (
    <div className="flex items-center gap-1">
      <span>0%</span>
      <div className="flex">
        {stops.map((s) => (
          <div
            key={s}
            style={{ background: rateToColor(s), width: 14, height: 10 }}
          />
        ))}
      </div>
      <span>100%</span>
    </div>
  );
}
