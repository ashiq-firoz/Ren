"use client";

import { Table2, BarChart3 } from "lucide-react";
import { useEffect, useState, type KeyboardEvent, type ReactNode } from "react";
import { dayLabel, formatNumber } from "@/lib/format";
import { cn } from "@/lib/utils";

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

/** Measure an element's width (re-renders on resize). */
export function useElementWidth<T extends HTMLElement>() {
  const [el, setEl] = useState<T | null>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.round(entry.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, [el]);
  return [setEl, width] as const;
}

/** Round up to a "nice" axis maximum (1, 2, 2.5, 5 × 10^n). */
export function niceMax(v: number): number {
  if (!Number.isFinite(v) || v <= 0) return 1;
  const exp = Math.floor(Math.log10(v));
  const base = 10 ** exp;
  const f = v / base;
  const nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
  return nf * base;
}

/** Column path with a 4px rounded data-end and a square baseline. */
function columnPath(x: number, y: number, w: number, h: number, r = 4) {
  if (h <= 0) return "";
  const rr = Math.min(r, w / 2, h);
  return `M${x},${y + h} L${x},${y + rr} Q${x},${y} ${x + rr},${y} L${x + w - rr},${y} Q${x + w},${y} ${x + w},${y + rr} L${x + w},${y + h} Z`;
}

export function ChartViewToggle({ view, onChange }: { view: "chart" | "table"; onChange: (v: "chart" | "table") => void }) {
  return (
    <div className="inline-flex rounded-md border border-border p-0.5" role="group" aria-label="Chart view">
      {(["chart", "table"] as const).map((v) => (
        <button
          key={v}
          type="button"
          aria-pressed={view === v}
          onClick={() => onChange(v)}
          title={v === "chart" ? "Chart" : "Table"}
          className={cn(
            "inline-flex size-6 items-center justify-center rounded-[5px] text-muted transition-colors",
            view === v ? "bg-surface-2 text-fg" : "hover:text-fg",
          )}
        >
          {v === "chart" ? <BarChart3 className="size-3.5" /> : <Table2 className="size-3.5" />}
          <span className="sr-only">{v === "chart" ? "Show chart" : "Show table"}</span>
        </button>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Usage: tokens (columns) + tasks (line) as small multiples           */
/* ------------------------------------------------------------------ */

export interface UsagePoint {
  date: string;
  tokens: number;
  tasks: number;
}

const LEFT = 44;
const RIGHT = 12;
const TOKENS_H = 132;
const TASKS_H = 76;
const GAP = 26;
const AXIS_H = 24;
const TOP = 8;

export function UsageChart({ data, className }: { data: UsagePoint[]; className?: string }) {
  const [setRef, width] = useElementWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const [view, setView] = useState<"chart" | "table">("chart");
  const n = data.length;
  const plotW = Math.max(0, width - LEFT - RIGHT);
  const band = n > 0 ? plotW / n : 0;
  const barW = Math.max(2, Math.min(24, band * 0.56));
  const tokMax = niceMax(Math.max(0, ...data.map((d) => d.tokens)));
  const taskMax = niceMax(Math.max(0, ...data.map((d) => d.tasks)));
  const tasksTop = TOP + TOKENS_H + GAP;
  const height = tasksTop + TASKS_H + AXIS_H;
  const cx = (i: number) => LEFT + band * i + band / 2;
  const ty = (v: number) => TOP + TOKENS_H - (v / tokMax) * TOKENS_H;
  const ky = (v: number) => tasksTop + TASKS_H - (v / taskMax) * TASKS_H;
  const linePath = data.map((d, i) => `${i === 0 ? "M" : "L"}${cx(i).toFixed(1)},${ky(d.tasks).toFixed(1)}`).join(" ");
  const labelEvery = band < 34 ? Math.ceil(34 / Math.max(band, 1)) : 1;
  const last = n - 1;

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (n === 0) return;
    if (e.key === "ArrowRight") {
      e.preventDefault();
      setHover((h) => (h === null ? 0 : Math.min(n - 1, h + 1)));
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      setHover((h) => (h === null ? n - 1 : Math.max(0, h - 1)));
    } else if (e.key === "Escape") {
      setHover(null);
    }
  };

  const hovered = hover !== null ? data[hover] : null;

  return (
    <div className={cn("relative", className)}>
      <div className="mb-2 flex items-center justify-between gap-3">
        <div className="flex items-center gap-4 text-xs text-muted">
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2.5 w-2 rounded-[2px] bg-accent" aria-hidden /> Tokens per day
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="h-0.5 w-3 rounded-full bg-fg" aria-hidden /> Tasks per day
          </span>
        </div>
        <ChartViewToggle view={view} onChange={setView} />
      </div>
      {view === "table" ? (
        <div className="max-h-[260px] overflow-auto rounded-lg border border-border">
          <table className="w-full text-[12.5px]">
            <thead className="sticky top-0 bg-surface-2 text-left text-[11px] text-subtle uppercase">
              <tr>
                <th className="px-3 py-1.5 font-medium">Date</th>
                <th className="px-3 py-1.5 text-right font-medium">Tokens</th>
                <th className="px-3 py-1.5 text-right font-medium">Tasks</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border tabular-nums">
              {data.map((d) => (
                <tr key={d.date}>
                  <td className="px-3 py-1.5">{dayLabel(d.date)}</td>
                  <td className="px-3 py-1.5 text-right">{formatNumber(d.tokens)}</td>
                  <td className="px-3 py-1.5 text-right">{formatNumber(d.tasks)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div
          ref={setRef}
          tabIndex={0}
          role="img"
          aria-label={`Tokens and tasks per day over the last ${n} days. Use the arrow keys to read values.`}
          onKeyDown={onKeyDown}
          onBlur={() => setHover(null)}
          onPointerLeave={() => setHover(null)}
          className="relative rounded-md outline-offset-4"
          style={{ height }}
        >
          {width > 0 && n > 0 && (
            <svg width={width} height={height} className="block overflow-visible">
              {/* Tokens panel grid + ticks */}
              {[0, 0.5, 1].map((t) => {
                const y = TOP + TOKENS_H - t * TOKENS_H;
                return (
                  <g key={`tg-${t}`}>
                    <line x1={LEFT} x2={width - RIGHT} y1={y} y2={y} stroke="var(--border)" strokeWidth={1} />
                    <text x={LEFT - 8} y={y} dy="0.32em" textAnchor="end" className="fill-[var(--text-subtle)] text-[10.5px] tabular-nums">
                      {formatNumber(tokMax * t, { compact: true })}
                    </text>
                  </g>
                );
              })}
              {/* Tasks panel grid + ticks */}
              {[0, 1].map((t) => {
                const y = tasksTop + TASKS_H - t * TASKS_H;
                return (
                  <g key={`kg-${t}`}>
                    <line x1={LEFT} x2={width - RIGHT} y1={y} y2={y} stroke="var(--border)" strokeWidth={1} />
                    <text x={LEFT - 8} y={y} dy="0.32em" textAnchor="end" className="fill-[var(--text-subtle)] text-[10.5px] tabular-nums">
                      {formatNumber(taskMax * t, { compact: true })}
                    </text>
                  </g>
                );
              })}
              {/* Crosshair */}
              {hover !== null && (
                <line
                  x1={cx(hover)}
                  x2={cx(hover)}
                  y1={TOP}
                  y2={tasksTop + TASKS_H}
                  stroke="var(--border-strong)"
                  strokeWidth={1}
                />
              )}
              {/* Token columns */}
              {data.map((d, i) => {
                const y = ty(d.tokens);
                const h = TOP + TOKENS_H - y;
                return (
                  <path
                    key={`b-${d.date}`}
                    d={columnPath(cx(i) - barW / 2, y, barW, h)}
                    fill="var(--accent)"
                    opacity={hover === null || hover === i ? 1 : 0.45}
                    className="transition-opacity duration-150"
                  />
                );
              })}
              {/* Tasks line + area wash */}
              <path
                d={`${linePath} L${cx(last)},${tasksTop + TASKS_H} L${cx(0)},${tasksTop + TASKS_H} Z`}
                fill="var(--text)"
                opacity={0.06}
              />
              <path d={linePath} fill="none" stroke="var(--text)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
              {/* End marker (and hovered marker) with surface ring */}
              {[last, ...(hover !== null && hover !== last ? [hover] : [])].map((i) => (
                <circle
                  key={`m-${i}`}
                  cx={cx(i)}
                  cy={ky(data[i].tasks)}
                  r={4}
                  fill="var(--text)"
                  stroke="var(--surface)"
                  strokeWidth={2}
                />
              ))}
              {/* Direct label at the line end */}
              <text
                x={Math.min(cx(last), width - RIGHT)}
                y={ky(data[last].tasks) - 10}
                textAnchor="end"
                className="fill-[var(--text-muted)] text-[10.5px] font-medium tabular-nums"
              >
                {formatNumber(data[last].tasks)} today
              </text>
              {/* X axis labels */}
              {data.map((d, i) =>
                i % labelEvery === 0 || i === last ? (
                  <text
                    key={`x-${d.date}`}
                    x={cx(i)}
                    y={height - 6}
                    textAnchor="middle"
                    className={cn("text-[10.5px]", hover === i ? "fill-[var(--text)]" : "fill-[var(--text-subtle)]")}
                  >
                    {dayLabel(d.date)}
                  </text>
                ) : null,
              )}
              {/* Panel labels */}
              <text x={LEFT} y={tasksTop - 9} className="fill-[var(--text-subtle)] text-[10.5px] font-medium">
                Tasks
              </text>
              {/* Hit targets: full band, both panels */}
              {data.map((d, i) => (
                <rect
                  key={`h-${d.date}`}
                  x={LEFT + band * i}
                  y={TOP}
                  width={band}
                  height={tasksTop + TASKS_H - TOP}
                  fill="transparent"
                  onPointerEnter={() => setHover(i)}
                  onPointerMove={() => setHover(i)}
                />
              ))}
            </svg>
          )}
          {hovered && hover !== null && (
            <div
              className="pointer-events-none absolute top-0 z-10 w-max min-w-[140px] -translate-x-1/2 rounded-lg border border-border bg-surface px-3 py-2 shadow-popover"
              style={{ left: Math.min(Math.max(cx(hover), 80), width - 80) }}
              role="status"
            >
              <p className="text-[11px] text-muted">{dayLabel(hovered.date)}</p>
              <p className="mt-1 flex items-center gap-2 text-[12.5px]">
                <span className="h-2.5 w-2 rounded-[2px] bg-accent" aria-hidden />
                <span className="font-semibold text-fg tabular-nums">{formatNumber(hovered.tokens)}</span>
                <span className="text-muted">tokens</span>
              </p>
              <p className="mt-0.5 flex items-center gap-2 text-[12.5px]">
                <span className="h-0.5 w-2.5 rounded-full bg-fg" aria-hidden />
                <span className="font-semibold text-fg tabular-nums">{formatNumber(hovered.tasks)}</span>
                <span className="text-muted">tasks</span>
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Horizontal bar list (one bar per entity, colored by the entity)     */
/* ------------------------------------------------------------------ */

export interface HBarItem {
  id: string;
  label: ReactNode;
  value: number;
  color?: string;
  valueLabel?: ReactNode;
  secondary?: ReactNode;
  title?: string;
}

export function HBarList({ items, max, className, emptyLabel = "No data yet" }: { items: HBarItem[]; max?: number; className?: string; emptyLabel?: string }) {
  const top = max ?? Math.max(1, ...items.map((i) => i.value));
  if (items.length === 0) return <p className="py-6 text-center text-xs text-muted">{emptyLabel}</p>;
  return (
    <ul className={cn("space-y-3", className)}>
      {items.map((item) => (
        <li key={item.id} className="group" title={item.title}>
          <div className="mb-1 flex items-center justify-between gap-3 text-[12.5px]">
            <span className="min-w-0 truncate text-fg">{item.label}</span>
            <span className="shrink-0 text-muted tabular-nums">
              <span className="font-medium text-fg">{item.valueLabel ?? formatNumber(item.value)}</span>
              {item.secondary && <span className="ml-1.5 text-subtle">{item.secondary}</span>}
            </span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-surface-2">
            <div
              className="h-full rounded-r-[4px] transition-[width,opacity] duration-300 group-hover:opacity-80"
              style={{
                width: `${top > 0 ? Math.max(item.value > 0 ? 2 : 0, (item.value / top) * 100) : 0}%`,
                background: item.color || "var(--accent)",
              }}
            />
          </div>
        </li>
      ))}
    </ul>
  );
}

/* ------------------------------------------------------------------ */
/* Compact column chart for daily series (e.g. alignment checks)       */
/* ------------------------------------------------------------------ */

export function MiniColumns({
  data,
  label,
  color = "var(--accent)",
  height = 64,
  format = (v: number) => formatNumber(v),
}: {
  data: { date: string; value: number }[];
  label: string;
  color?: string;
  height?: number;
  format?: (v: number) => string;
}) {
  const [setRef, width] = useElementWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const n = data.length;
  const band = n ? width / n : 0;
  const barW = Math.max(2, Math.min(16, band * 0.6));
  const max = niceMax(Math.max(0, ...data.map((d) => d.value)));
  const h = height;
  const hovered = hover !== null ? data[hover] : null;
  return (
    <div ref={setRef} className="relative" style={{ height: h + 18 }} onPointerLeave={() => setHover(null)} role="img" aria-label={label}>
      {width > 0 && (
        <svg width={width} height={h + 18} className="block">
          <line x1={0} x2={width} y1={h} y2={h} stroke="var(--border)" strokeWidth={1} />
          {data.map((d, i) => {
            const bh = (d.value / max) * (h - 4);
            return (
              <g key={d.date}>
                <path
                  d={columnPath(band * i + band / 2 - barW / 2, h - bh, barW, bh, 3)}
                  fill={color}
                  opacity={hover === null || hover === i ? 1 : 0.45}
                />
                <rect x={band * i} y={0} width={band} height={h} fill="transparent" onPointerEnter={() => setHover(i)} />
              </g>
            );
          })}
          {n > 0 && (
            <>
              <text x={0} y={h + 14} className="fill-[var(--text-subtle)] text-[10px]">
                {dayLabel(data[0].date)}
              </text>
              <text x={width} y={h + 14} textAnchor="end" className="fill-[var(--text-subtle)] text-[10px]">
                {dayLabel(data[n - 1].date)}
              </text>
            </>
          )}
        </svg>
      )}
      {hovered && hover !== null && (
        <div
          className="pointer-events-none absolute -top-2 z-10 w-max -translate-x-1/2 -translate-y-full rounded-md border border-border bg-surface px-2 py-1 text-[11.5px] shadow-popover"
          style={{ left: Math.min(Math.max(band * hover + band / 2, 50), width - 50) }}
        >
          <span className="font-semibold text-fg tabular-nums">{format(hovered.value)}</span>{" "}
          <span className="text-muted">· {dayLabel(hovered.date)}</span>
        </div>
      )}
    </div>
  );
}
