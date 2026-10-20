"use client";

import Link from "next/link";
import { useState } from "react";
import { LogoMark } from "@/components/brand/logo";
import { AgentAvatar } from "@/components/ui/avatar";
import { formatPercent } from "@/lib/format";
import type { Agent } from "@/lib/types";
import { cn } from "@/lib/utils";

const W = 1000;
const H = 620;
const CX = W / 2;
const CY = H / 2;

/**
 * Ren in the center, specialists on a ring. An edge animates while that agent
 * is running work (live via the task stream).
 */
export function SwarmMap({ agents, running }: { agents: Agent[]; running: Map<string, number> }) {
  const [hover, setHover] = useState<string | null>(null);
  const orchestrator = agents.find((a) => a.is_orchestrator);
  const ring = agents.filter((a) => !a.is_orchestrator);
  const n = ring.length;
  const rx = n <= 4 ? 250 : 330;
  const ry = n <= 4 ? 190 : 225;
  const nodes = ring.map((a, i) => {
    const angle = (i / Math.max(1, n)) * Math.PI * 2 - Math.PI / 2;
    return { agent: a, x: CX + rx * Math.cos(angle), y: CY + ry * Math.sin(angle) };
  });
  const totalActive = Array.from(running.values()).reduce((s, v) => s + v, 0);

  return (
    <div className="relative overflow-hidden rounded-card border border-border bg-surface">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_center,color-mix(in_srgb,var(--accent)_7%,transparent),transparent_60%)]" />
      <div className="relative mx-auto w-full max-w-[1100px]" style={{ aspectRatio: `${W} / ${H}` }}>
        <svg viewBox={`0 0 ${W} ${H}`} className="absolute inset-0 size-full" aria-hidden>
          {/* orbit */}
          <ellipse cx={CX} cy={CY} rx={rx} ry={ry} fill="none" stroke="var(--border)" strokeWidth={1} />
          {nodes.map(({ agent, x, y }) => {
            const active = (running.get(agent.id) ?? 0) > 0;
            const highlighted = hover === agent.id;
            return (
              <g key={agent.id}>
                <line
                  x1={CX}
                  y1={CY}
                  x2={x}
                  y2={y}
                  stroke={active ? agent.color || "var(--accent)" : "var(--border-strong)"}
                  strokeWidth={active ? 2 : highlighted ? 1.5 : 1}
                  strokeOpacity={active ? 0.9 : highlighted ? 0.9 : 0.6}
                />
                {active && (
                  <line
                    x1={CX}
                    y1={CY}
                    x2={x}
                    y2={y}
                    stroke="var(--surface)"
                    strokeWidth={2.5}
                    strokeDasharray="3 17"
                    strokeLinecap="round"
                    className="animate-edge-flow"
                  />
                )}
              </g>
            );
          })}
        </svg>

        {/* Ren */}
        <div className="absolute flex -translate-x-1/2 -translate-y-1/2 flex-col items-center gap-2" style={{ left: "50%", top: "50%" }}>
          <Link
            href={orchestrator ? `/agents/${orchestrator.id}` : "/agents"}
            className="relative flex size-[76px] items-center justify-center rounded-2xl bg-brand text-brand-fg shadow-popover ring-4 ring-surface"
            aria-label="Ren, the orchestrator"
          >
            {totalActive > 0 && <span className="absolute inset-0 animate-ping rounded-2xl bg-[color-mix(in_srgb,var(--accent)_25%,transparent)] [animation-duration:2.4s]" />}
            <LogoMark className="relative w-[52px]" />
          </Link>
          <div className="text-center">
            <p className="font-serif-display text-xl leading-none text-fg">Ren</p>
            <p className="mt-1 text-[11px] text-muted">{totalActive > 0 ? `${totalActive} tasks in flight` : "Orchestrator"}</p>
          </div>
        </div>

        {/* Specialists */}
        {nodes.map(({ agent, x, y }) => {
          const active = running.get(agent.id) ?? 0;
          return (
            <Link
              key={agent.id}
              href={`/agents/${agent.id}`}
              onMouseEnter={() => setHover(agent.id)}
              onMouseLeave={() => setHover(null)}
              onFocus={() => setHover(agent.id)}
              onBlur={() => setHover(null)}
              className="group absolute flex w-[132px] -translate-x-1/2 -translate-y-[22px] flex-col items-center gap-1.5 text-center"
              style={{ left: `${(x / W) * 100}%`, top: `${(y / H) * 100}%` }}
            >
              <span className={cn("relative rounded-xl ring-4 ring-surface transition-transform group-hover:scale-105", !agent.enabled && "opacity-50 grayscale")}>
                <AgentAvatar agent={agent} size="lg" />
                {active > 0 && (
                  <span className="absolute -top-1.5 -right-1.5 flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-accent px-1 text-[10px] font-bold text-accent-fg ring-2 ring-surface">
                    {active}
                  </span>
                )}
              </span>
              <span className="w-full truncate text-[12px] font-medium text-fg">{agent.name}</span>
              <span className="-mt-1 w-full truncate text-[10.5px] text-muted">
                {active > 0 ? "Working" : agent.enabled ? agent.role : "Disabled"}
              </span>
              {hover === agent.id && agent.stats && (
                <span className="pointer-events-none absolute top-full z-10 mt-1 w-[180px] rounded-lg border border-border bg-surface px-3 py-2 text-left text-[11.5px] shadow-popover">
                  <span className="block text-muted">
                    <span className="font-semibold text-fg">{agent.stats.tasks_total}</span> tasks ·{" "}
                    <span className="font-semibold text-fg">{formatPercent(agent.stats.success_rate)}</span> success
                  </span>
                  <span className="mt-0.5 block text-muted">
                    Alignment <span className="font-semibold text-fg">{formatPercent(agent.stats.alignment_pass_rate)}</span>
                  </span>
                </span>
              )}
            </Link>
          );
        })}
      </div>
      <div className="relative flex flex-wrap items-center justify-between gap-2 border-t border-border px-4 py-2.5 text-[11.5px] text-muted">
        <span className="inline-flex items-center gap-2">
          <svg width="28" height="6" aria-hidden>
            <line x1="0" y1="3" x2="28" y2="3" stroke="var(--accent)" strokeWidth="2" />
          </svg>
          Animated link = agent is running a task (live)
        </span>
        <span>
          {n} specialists · {Array.from(running.values()).filter((v) => v > 0).length} working
        </span>
      </div>
    </div>
  );
}
