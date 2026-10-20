"use client";

import { Bot, LayoutGrid, Network, Plus } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useMemo } from "react";
import { AgentCard } from "@/components/agents/agent-card";
import { SwarmMap } from "@/components/agents/swarm-map";
import { PageContainer, PageHeader } from "@/components/shell/page-header";
import { ButtonLink } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { SegmentedControl } from "@/components/ui/controls";
import { EmptyState, ErrorState, Skeleton, SkeletonCards } from "@/components/ui/feedback";
import { useAgents, useTasks } from "@/hooks/queries";
import { useRole } from "@/providers/auth";

export default function AgentsPage() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const view = params.get("view") === "map" ? "map" : "grid";
  const { isAdmin } = useRole();
  const { data, isLoading, error, refetch } = useAgents();
  // Live running tasks (kept current by the stream) drive the "working" indicators.
  const runningTasks = useTasks({ status: "running", limit: 200 });

  const running = useMemo(() => {
    const m = new Map<string, number>();
    for (const t of runningTasks.data?.items ?? []) m.set(t.agent_id, (m.get(t.agent_id) ?? 0) + 1);
    if (!runningTasks.data) {
      for (const a of data?.items ?? []) if (a.stats?.active_tasks) m.set(a.id, a.stats.active_tasks);
    }
    return m;
  }, [runningTasks.data, data]);

  const agents = useMemo(
    () =>
      (data?.items ?? []).slice().sort((a, b) => Number(b.is_orchestrator) - Number(a.is_orchestrator) || Number(b.enabled) - Number(a.enabled) || a.name.localeCompare(b.name)),
    [data],
  );

  return (
    <PageContainer wide={view === "map"}>
      <PageHeader
        title="Agents"
        description="The specialists in your swarm. Ren decides who does what; you decide what each one is allowed to do."
        actions={
          <>
            <SegmentedControl
              aria-label="View"
              value={view}
              onChange={(v) => router.replace(v === "map" ? `${pathname}?view=map` : pathname, { scroll: false })}
              options={[
                { value: "grid", label: "Grid", icon: <LayoutGrid /> },
                { value: "map", label: "Swarm map", icon: <Network /> },
              ]}
            />
            {isAdmin && (
              <ButtonLink href="/agents/new" variant="primary" icon={<Plus className="size-4" />}>
                New agent
              </ButtonLink>
            )}
          </>
        }
      />

      {isLoading ? (
        view === "map" ? (
          <Skeleton className="aspect-[1000/620] h-auto w-full rounded-card" />
        ) : (
          <SkeletonCards count={6} />
        )
      ) : error && !data ? (
        <Card>
          <ErrorState error={error} onRetry={() => refetch()} />
        </Card>
      ) : agents.length === 0 ? (
        <Card>
          <EmptyState
            icon={<Bot />}
            title="No agents yet"
            description="Create a specialist with its own instructions, tools, knowledge and autonomy."
            action={
              isAdmin ? (
                <ButtonLink href="/agents/new" variant="primary">
                  Create an agent
                </ButtonLink>
              ) : undefined
            }
          />
        </Card>
      ) : view === "map" ? (
        <SwarmMap agents={agents.filter((a) => a.enabled || a.is_orchestrator)} running={running} />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {agents.map((a) => (
            <AgentCard key={a.id} agent={a} running={runningTasks.data ? running.get(a.id) ?? 0 : undefined} />
          ))}
        </div>
      )}
    </PageContainer>
  );
}
