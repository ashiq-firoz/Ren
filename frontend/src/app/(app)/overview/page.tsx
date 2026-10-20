"use client";

import {
  Activity,
  ArrowUpRight,
  CheckCircle2,
  Coins,
  FileText,
  ListChecks,
  MessageSquarePlus,
  ShieldCheck,
  ShieldQuestion,
} from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { HBarList, UsageChart } from "@/components/charts/charts";
import { ActorAvatar, actorName, AuditTarget, humanizeAction } from "@/components/domain/audit";
import { RiskBadge } from "@/components/domain/status";
import { TaskTable } from "@/components/domain/task-table";
import { PageContainer, PageHeader } from "@/components/shell/page-header";
import { AgentAvatar } from "@/components/ui/avatar";
import { StatusDot } from "@/components/ui/badge";
import { ButtonLink } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { EmptyState, ErrorState, Skeleton, SkeletonRows } from "@/components/ui/feedback";
import { useAgents, useAgentsMap, useDashboard } from "@/hooks/queries";
import { formatNumber, formatPercent, timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";

function Kpi({
  label,
  value,
  sub,
  icon,
  href,
  tone,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  icon: ReactNode;
  href?: string;
  tone?: "warning" | "danger" | "success";
}) {
  const body = (
    <div className="flex h-full flex-col justify-between gap-3 rounded-card border border-border bg-surface p-4 transition-colors group-hover:border-border-strong">
      <div className="flex items-center justify-between text-muted">
        <span className="text-[12.5px]">{label}</span>
        <span
          className={cn(
            "flex size-7 items-center justify-center rounded-md [&_svg]:size-3.5",
            tone === "warning" ? "tint-warning" : tone === "danger" ? "tint-danger" : tone === "success" ? "tint-success" : "bg-surface-2",
          )}
        >
          {icon}
        </span>
      </div>
      <div>
        <p className="text-[28px] leading-none font-semibold tracking-tight text-fg">{value}</p>
        {sub && <p className="mt-1.5 text-xs text-muted">{sub}</p>}
      </div>
    </div>
  );
  return href ? (
    <Link href={href} className="group block rounded-card">
      {body}
    </Link>
  ) : (
    <div className="group">{body}</div>
  );
}

function LiveSwarm() {
  const { data, isLoading } = useAgents();
  const agents = (data?.items ?? []).filter((a) => a.enabled);
  const sorted = agents.slice().sort((a, b) => (b.stats?.active_tasks ?? 0) - (a.stats?.active_tasks ?? 0) || Number(b.is_orchestrator) - Number(a.is_orchestrator));
  const working = agents.filter((a) => (a.stats?.active_tasks ?? 0) > 0).length;
  return (
    <Card>
      <CardHeader
        title="Live swarm"
        description={isLoading ? "Loading agents…" : working ? `${working} of ${agents.length} agents working` : "All agents idle — ready for work"}
        actions={
          <Link href="/agents?view=map" className="text-xs font-medium text-muted hover:text-fg">
            Swarm map →
          </Link>
        }
      />
      <div className="scrollbar-none flex gap-2 overflow-x-auto p-3">
        {isLoading
          ? Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-[52px] w-40 shrink-0 rounded-card" />)
          : sorted.map((a) => {
              const active = a.stats?.active_tasks ?? 0;
              return (
                <Link
                  key={a.id}
                  href={`/agents/${a.id}`}
                  className="flex shrink-0 items-center gap-2.5 rounded-card border border-border bg-bg/40 py-2 pr-3.5 pl-2 transition-colors hover:border-border-strong"
                >
                  <AgentAvatar agent={a} size="md" />
                  <span className="min-w-0">
                    <span className="block max-w-[140px] truncate text-[12.5px] font-medium text-fg">{a.name}</span>
                    <span className="flex items-center gap-1.5 text-[11px] text-muted">
                      <StatusDot tone={active > 0 ? "running" : "idle"} pulse={active > 0} size={6} />
                      {active > 0 ? `${active} active` : "Idle"}
                    </span>
                  </span>
                </Link>
              );
            })}
      </div>
    </Card>
  );
}

export default function OverviewPage() {
  const { data, isLoading, error, refetch } = useDashboard();
  const agentsMap = useAgentsMap();
  const s = data?.stats;

  return (
    <PageContainer>
      <PageHeader
        title="Overview"
        description="What your swarm is doing right now, and how it has been performing."
        actions={
          <ButtonLink href="/chat" variant="primary" icon={<MessageSquarePlus className="size-4" />}>
            New chat
          </ButtonLink>
        }
      />

      {error && !data ? (
        <Card>
          <ErrorState error={error} onRetry={() => refetch()} />
        </Card>
      ) : (
        <div className="space-y-5">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
            {isLoading || !s ? (
              Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-[112px] rounded-card" />)
            ) : (
              <>
                <Kpi label="Active tasks" value={formatNumber(s.active_tasks)} sub={`${formatNumber(s.agents)} agents`} icon={<Activity />} href="/tasks?status=running" />
                <Kpi
                  label="Completed today"
                  value={formatNumber(s.completed_today)}
                  sub={s.failed_today ? `${s.failed_today} failed` : "No failures"}
                  icon={<CheckCircle2 />}
                  tone={s.failed_today ? "danger" : "success"}
                  href="/tasks?status=completed"
                />
                <Kpi
                  label="Pending approvals"
                  value={formatNumber(s.pending_approvals)}
                  sub={s.pending_approvals ? "Waiting for a decision" : "Nothing waiting"}
                  icon={<ShieldQuestion />}
                  tone={s.pending_approvals ? "warning" : undefined}
                  href="/approvals"
                />
                <Kpi
                  label="Alignment pass rate"
                  value={formatPercent(s.alignment_pass_rate)}
                  sub="Policy checks"
                  icon={<ShieldCheck />}
                  href="/policies"
                />
                <Kpi label="Documents" value={formatNumber(s.documents)} sub="In knowledge" icon={<FileText />} href="/knowledge" />
                <Kpi
                  label="Tokens today"
                  value={formatNumber(s.tokens_today, { compact: true })}
                  sub={`${formatNumber(s.conversations)} conversations`}
                  icon={<Coins />}
                />
              </>
            )}
          </div>

          <LiveSwarm />

          <div className="grid gap-5 lg:grid-cols-5">
            <Card className="lg:col-span-3">
              <CardHeader title="Usage" description="Last 14 days" />
              <div className="p-4">
                {isLoading || !data ? (
                  <Skeleton className="h-[280px] w-full rounded-lg" />
                ) : data.usage_series.length === 0 ? (
                  <p className="py-16 text-center text-sm text-muted">No usage recorded yet.</p>
                ) : (
                  <UsageChart data={data.usage_series} />
                )}
              </div>
            </Card>
            <Card className="lg:col-span-2">
              <CardHeader title="Agent utilization" description="Tasks handled, last 14 days" />
              <div className="p-4">
                {isLoading || !data ? (
                  <div className="space-y-4">
                    {Array.from({ length: 5 }).map((_, i) => (
                      <Skeleton key={i} className="h-7 w-full" />
                    ))}
                  </div>
                ) : (
                  <HBarList
                    emptyLabel="No agent activity yet."
                    items={data.agent_utilization
                      .slice()
                      .sort((a, b) => b.tasks - a.tasks)
                      .map((u) => ({
                        id: u.agent_id,
                        label: (
                          <span className="inline-flex items-center gap-2">
                            <AgentAvatar agent={agentsMap.get(u.agent_id)} name={u.name} color={u.color} size="xs" />
                            <span className="truncate">{u.name}</span>
                            {u.active > 0 && <StatusDot tone="running" pulse size={6} label={`${u.active} active`} />}
                          </span>
                        ),
                        value: u.tasks,
                        valueLabel: `${formatNumber(u.tasks)} ${u.tasks === 1 ? "task" : "tasks"}`,
                        secondary: `${formatNumber(u.tokens, { compact: true })} tok`,
                        color: u.color,
                        title: `${u.name}: ${u.tasks} tasks, ${u.tokens} tokens, ${u.active} active`,
                      }))}
                  />
                )}
              </div>
            </Card>
          </div>

          <div className="grid gap-5 lg:grid-cols-5">
            <Card className="overflow-hidden lg:col-span-3">
              <CardHeader
                title="Recent tasks"
                actions={
                  <Link href="/tasks" className="inline-flex items-center gap-1 text-xs font-medium text-muted hover:text-fg">
                    All tasks <ArrowUpRight className="size-3" />
                  </Link>
                }
              />
              {isLoading || !data ? (
                <SkeletonRows rows={6} />
              ) : data.recent_tasks.length === 0 ? (
                <EmptyState
                  compact
                  icon={<ListChecks />}
                  title="No tasks yet"
                  description="Ask Ren for something in chat — delegated work shows up here."
                  action={
                    <ButtonLink href="/chat" size="sm" variant="primary">
                      Open chat
                    </ButtonLink>
                  }
                />
              ) : (
                <TaskTable tasks={data.recent_tasks} compact />
              )}
            </Card>
            <Card className="overflow-hidden lg:col-span-2">
              <CardHeader
                title="Activity"
                actions={
                  <Link href="/activity" className="inline-flex items-center gap-1 text-xs font-medium text-muted hover:text-fg">
                    Audit log <ArrowUpRight className="size-3" />
                  </Link>
                }
              />
              {isLoading || !data ? (
                <SkeletonRows rows={6} />
              ) : data.recent_activity.length === 0 ? (
                <p className="px-4 py-10 text-center text-xs text-muted">No activity yet.</p>
              ) : (
                <ul className="divide-y divide-border">
                  {data.recent_activity.map((e) => (
                    <li key={e.id} className="flex items-start gap-3 px-4 py-2.5">
                      <ActorAvatar entry={e} agent={e.agent_id ? agentsMap.get(e.agent_id) : undefined} />
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-[12.5px] text-fg">
                          <span className="font-medium">{actorName(e)}</span>{" "}
                          <span className="text-muted">{humanizeAction(e.action).toLowerCase()}</span>
                        </p>
                        <p className="mt-0.5 flex min-w-0 items-center gap-1.5 text-[11.5px]">
                          <AuditTarget entry={e} />
                        </p>
                      </div>
                      <div className="flex shrink-0 flex-col items-end gap-1">
                        <span className="text-[11px] text-subtle">{timeAgo(e.created_at, { short: true })}</span>
                        {e.risk && e.risk !== "low" && <RiskBadge risk={e.risk} size="xs" label={false} />}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </div>
        </div>
      )}
    </PageContainer>
  );
}
