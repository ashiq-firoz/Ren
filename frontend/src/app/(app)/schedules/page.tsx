"use client";

import { CalendarClock, History, MessageSquare, Plus } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState, type ReactNode } from "react";
import { ScheduleDrawer } from "@/components/schedules/schedule-drawer";
import { ScheduleEditor } from "@/components/schedules/schedule-editor";
import {
  LastRun,
  NextRun,
  ScheduleActions,
  ScheduleAgent,
  ScheduleStatusBadge,
  TimeZoneChip,
} from "@/components/schedules/schedule-parts";
import { PageContainer, PageHeader } from "@/components/shell/page-header";
import { Button, ButtonLink } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, ErrorState, Skeleton, SkeletonRows } from "@/components/ui/feedback";
import { Tabs } from "@/components/ui/tabs";
import { useAgentsMap, useSchedules } from "@/hooks/queries";
import { useBrowserTimeZone } from "@/hooks/use-browser";
import { formatTimeZone } from "@/lib/format";
import { scheduleSummaryWithoutZone } from "@/lib/schedules";
import type { Agent, ScheduledJob } from "@/lib/types";
import { pluralize } from "@/lib/utils";
import { useRole } from "@/providers/auth";

type Tab = "active" | "history";

/** Desktop columns: schedule · next run · last run · actions. */
const ROW_GRID = "md:grid-cols-[minmax(0,1fr)_200px_150px_132px]";

function Cell({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0 text-[13px]">
      <p className="mb-0.5 text-[10.5px] font-medium tracking-wide text-subtle uppercase md:hidden">{label}</p>
      {children}
    </div>
  );
}

function ScheduleRow({
  job,
  agent,
  onOpen,
  onEdit,
}: {
  job: ScheduledJob;
  agent?: Agent;
  onOpen: (job: ScheduledJob) => void;
  onEdit: (job: ScheduledJob) => void;
}) {
  const history = job.status !== "scheduled" && job.status !== "paused";
  return (
    <li
      onClick={() => onOpen(job)}
      className={`grid cursor-pointer gap-x-6 gap-y-3 px-4 py-3.5 transition-colors hover:bg-surface-2/60 md:items-center ${ROW_GRID}`}
    >
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2">
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onOpen(job);
            }}
            className="min-w-0 truncate rounded-sm text-left text-[14px] font-medium text-fg hover:underline"
          >
            {job.title}
          </button>
          {(job.status === "paused" || history) && <ScheduleStatusBadge status={job.status} />}
        </div>
        <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
          <ScheduleAgent job={job} agent={agent} size="xs" className="max-w-[220px]" />
          <span aria-hidden className="text-subtle">
            ·
          </span>
          <span className="min-w-0">{scheduleSummaryWithoutZone(job)}</span>
          <TimeZoneChip timeZone={job.timezone} />
        </div>
      </div>
      {/* Two columns on phones; real grid columns on desktop. */}
      <div className="grid grid-cols-2 gap-x-4 gap-y-3 md:contents">
        <Cell label="Next run">
          <NextRun job={job} />
        </Cell>
        <Cell label="Last run">
          <div className="flex min-w-0 flex-col gap-0.5">
            <LastRun job={job} />
            {job.run_count > 0 && <span className="text-[11px] text-subtle tabular-nums">{pluralize(job.run_count, "run")}</span>}
          </div>
        </Cell>
      </div>
      <ScheduleActions job={job} onEdit={onEdit} className="-ml-1.5 md:ml-0 md:justify-end" />
    </li>
  );
}

function ScheduleList({
  tab,
  onOpen,
  onEdit,
  onCreate,
}: {
  tab: Tab;
  onOpen: (job: ScheduledJob) => void;
  onEdit: (job: ScheduledJob) => void;
  onCreate: () => void;
}) {
  const { canWrite } = useRole();
  const agents = useAgentsMap();
  const { data, isLoading, error, refetch } = useSchedules({ status: tab });
  const items = data?.items ?? [];

  if (isLoading)
    return (
      <Card>
        <SkeletonRows rows={5} />
      </Card>
    );
  if (error && !data)
    return (
      <Card>
        <ErrorState error={error} onRetry={() => void refetch()} />
      </Card>
    );
  if (items.length === 0)
    return (
      <Card>
        {tab === "active" ? (
          <EmptyState
            icon={<CalendarClock />}
            title="Nothing scheduled yet"
            description="Ask Ren, e.g. “send me a tech & AI newsletter every weekday at 9am”, or create one here."
            action={
              <>
                {canWrite && (
                  <Button size="sm" variant="primary" icon={<Plus className="size-3.5" />} onClick={onCreate}>
                    New schedule
                  </Button>
                )}
                <ButtonLink size="sm" href="/chat" icon={<MessageSquare className="size-3.5" />}>
                  Ask Ren
                </ButtonLink>
              </>
            }
          />
        ) : (
          <EmptyState
            icon={<History />}
            title="No past schedules"
            description="One-off schedules that already ran, and schedules you delete, are kept here for the record."
          />
        )}
      </Card>
    );

  return (
    <Card className="overflow-hidden">
      <div
        aria-hidden
        className={`hidden gap-x-6 border-b border-border px-4 py-2.5 text-[11px] font-medium tracking-wide text-subtle uppercase md:grid ${ROW_GRID}`}
      >
        <span>Schedule</span>
        <span>Next run</span>
        <span>Last run</span>
        <span />
      </div>
      <ul className="divide-y divide-border">
        {items.map((job) => (
          <ScheduleRow key={job.id} job={job} agent={job.agent_id ? agents.get(job.agent_id) : undefined} onOpen={onOpen} onEdit={onEdit} />
        ))}
      </ul>
    </Card>
  );
}

function SchedulesView() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const { canWrite } = useRole();
  const browserTz = useBrowserTimeZone();
  // { job: null } = new schedule; null = closed.
  const [editor, setEditor] = useState<{ job: ScheduledJob | null } | null>(null);

  const tab: Tab = params.get("tab") === "history" ? "history" : "active";
  const selectedId = params.get("id");
  const active = useSchedules({ status: "active" });
  const history = useSchedules({ status: "history" });
  const selected = [...(active.data?.items ?? []), ...(history.data?.items ?? [])].find((j) => j.id === selectedId) ?? null;

  const setParam = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params.toString());
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || (k === "tab" && v === "active")) next.delete(k);
      else next.set(k, v);
    }
    const s = next.toString();
    router.replace(`${pathname}${s ? `?${s}` : ""}`, { scroll: false });
  };

  return (
    <PageContainer>
      <PageHeader
        title="Schedules"
        description={`Recurring and one-off work for your agents, plus reminders. Times are shown in your time zone (${formatTimeZone(browserTz)}).`}
        actions={
          canWrite && (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setEditor({ job: null })}>
              New schedule
            </Button>
          )
        }
      />

      <Tabs<Tab>
        aria-label="Schedules"
        idPrefix="schedules-tab"
        value={tab}
        onValueChange={(v) => setParam({ tab: v })}
        className="mb-5"
        items={[
          { value: "active", label: "Active", count: active.data ? active.data.total : null },
          { value: "history", label: "History", count: history.data ? history.data.total : null },
        ]}
      />

      <div role="tabpanel" aria-labelledby={`schedules-tab-${tab}`}>
        <ScheduleList
          tab={tab}
          onOpen={(job) => setParam({ id: job.id })}
          onEdit={(job) => setEditor({ job })}
          onCreate={() => setEditor({ job: null })}
        />
      </div>

      <ScheduleDrawer
        id={selectedId}
        initial={selected}
        onClose={() => setParam({ id: null })}
        onEdit={(job) => setEditor({ job })}
      />
      {editor && <ScheduleEditor job={editor.job} onClose={() => setEditor(null)} />}
    </PageContainer>
  );
}

function SchedulesFallback() {
  return (
    <PageContainer>
      <Skeleton className="h-9 w-48" />
      <Skeleton className="mt-3 h-4 w-80 max-w-full" />
      <Skeleton className="mt-8 h-56 w-full rounded-card" />
    </PageContainer>
  );
}

export default function SchedulesPage() {
  // useSearchParams (tab / open schedule) needs a Suspense boundary for prerendering.
  return (
    <Suspense fallback={<SchedulesFallback />}>
      <SchedulesView />
    </Suspense>
  );
}
