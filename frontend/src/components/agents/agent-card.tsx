"use client";

import { Activity, Wrench } from "lucide-react";
import Link from "next/link";
import { AutonomyPill } from "@/components/domain/status";
import { AgentAvatar } from "@/components/ui/avatar";
import { Badge, StatusDot } from "@/components/ui/badge";
import { formatPercent } from "@/lib/format";
import type { Agent } from "@/lib/types";
import { cn } from "@/lib/utils";

export function AgentCard({ agent, running }: { agent: Agent; running?: number }) {
  const active = running ?? agent.stats?.active_tasks ?? 0;
  const s = agent.stats;
  return (
    <Link
      href={`/agents/${agent.id}`}
      className={cn(
        "group flex h-full flex-col rounded-card border bg-surface p-4 transition-[border-color,transform] duration-150 hover:-translate-y-px hover:border-border-strong",
        agent.is_orchestrator ? "border-fg/60" : "border-border",
        !agent.enabled && "opacity-70",
      )}
    >
      <div className="flex items-start gap-3">
        <AgentAvatar agent={agent} size="lg" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="truncate text-[14px] font-semibold text-fg">{agent.name}</h3>
            {agent.is_orchestrator && (
              <Badge tone="solid" size="xs">
                Orchestrator
              </Badge>
            )}
            {!agent.enabled && (
              <Badge tone="outline" size="xs">
                Disabled
              </Badge>
            )}
          </div>
          <p className="truncate text-xs text-muted">{agent.role}</p>
        </div>
        {active > 0 && (
          <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full tint-accent px-2 py-0.5 text-[11px] font-medium">
            <StatusDot tone="running" pulse size={6} />
            {active} active
          </span>
        )}
      </div>
      <p className="mt-3 line-clamp-3 flex-1 text-[12.5px] leading-relaxed text-muted">{agent.description || "No description."}</p>
      <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-border pt-3 text-[11.5px] text-muted">
        <span className="inline-flex items-center gap-1" title="Tool groups">
          <Wrench className="size-3" /> {agent.tool_groups.length} tool {agent.tool_groups.length === 1 ? "group" : "groups"}
        </span>
        {!agent.is_orchestrator && <AutonomyPill autonomy={agent.autonomy} />}
        {s && s.tasks_total > 0 && (
          <span className="ml-auto inline-flex items-center gap-1" title={`${s.tasks_completed} of ${s.tasks_total} tasks succeeded`}>
            <Activity className="size-3" /> {formatPercent(s.success_rate)} success
          </span>
        )}
      </div>
    </Link>
  );
}
