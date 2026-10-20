"use client";

import { CalendarClock, Download, FileText, History, Repeat } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { TaskStatusPill } from "@/components/domain/status";
import { FileKindIcon } from "@/components/files/file-icon";
import { ViewFileButton } from "@/components/files/file-link";
import { IconButton } from "@/components/ui/button";
import { EmptyState, ErrorState, Skeleton, SkeletonText } from "@/components/ui/feedback";
import { Markdown } from "@/components/ui/markdown";
import { Drawer } from "@/components/ui/overlay";
import { useAgentsMap, useSchedule, useSchedulePreview } from "@/hooks/queries";
import { useBrowserTimeZone } from "@/hooks/use-browser";
import { ApiError } from "@/lib/api";
import { downloadFile } from "@/lib/files";
import { durationBetween, formatBytes, formatDateTime, formatDuration, formatTime, isSameDay } from "@/lib/format";
import { isScheduleActive, scheduleSummary } from "@/lib/schedules";
import type { ScheduledJob, ScheduleRun } from "@/lib/types";
import { Countdown, LastRun, NextRun, ScheduleActions, ScheduleAgent, ScheduleStatusBadge, TimeZoneChip } from "./schedule-parts";

function DrawerSection({ title, icon, aside, children }: { title: ReactNode; icon: ReactNode; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="space-y-2.5">
      <div className="flex items-center justify-between gap-2">
        <h3 className="flex items-center gap-1.5 text-[11px] font-medium tracking-[0.06em] text-subtle uppercase [&_svg]:size-3.5">
          {icon}
          {title}
        </h3>
        {aside}
      </div>
      {children}
    </section>
  );
}

function Item({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] font-medium tracking-wide text-subtle uppercase">{label}</dt>
      <dd className="mt-0.5 min-w-0 text-fg">{children}</dd>
    </div>
  );
}

function RunTimes({ times }: { times: string[] }) {
  if (times.length === 0) return <p className="text-xs text-muted">No upcoming runs.</p>;
  return (
    <ol className="divide-y divide-border rounded-card border border-border bg-surface text-[13px]">
      {times.map((iso, i) => (
        <li key={iso} className="flex items-center justify-between gap-3 px-3 py-2">
          <span className="text-fg">{formatDateTime(iso, { weekday: true })}</span>
          {i === 0 && (
            <span className="text-xs text-subtle">
              <Countdown value={iso} />
            </span>
          )}
        </li>
      ))}
    </ol>
  );
}

/** Next runs: from /schedules/preview for recurring jobs, the run_at itself for one-offs. */
function UpcomingRuns({ job }: { job: ScheduledJob }) {
  const browserTz = useBrowserTimeZone();
  const active = isScheduleActive(job.status);
  const recurring = !!job.cron;
  const preview = useSchedulePreview({ cron: job.cron, timezone: job.timezone, count: 5 }, active && recurring);
  if (!active) return null;

  let list: ReactNode;
  if (!recurring) list = <RunTimes times={[job.run_at]} />;
  else if (preview.isLoading || preview.isPlaceholderData)
    list = (
      <div className="space-y-2 rounded-card border border-border bg-surface p-3">
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} className="h-3.5" style={{ width: `${70 - i * 10}%` }} />
        ))}
      </div>
    );
  else if (preview.error) list = <p className="text-xs text-muted">Couldn&apos;t load the upcoming runs.</p>;
  else if (preview.data && !preview.data.valid) list = <p className="text-xs text-danger">{preview.data.error || "This schedule isn't valid."}</p>;
  else list = <RunTimes times={preview.data?.next ?? []} />;

  return (
    <div className="space-y-1.5">
      <p className="text-xs font-medium text-muted">
        {job.status === "paused" ? "Upcoming runs — on hold while paused" : recurring ? "Next runs" : "Runs once"}
        {job.timezone !== browserTz && <span className="font-normal text-subtle"> · shown in your time zone</span>}
      </p>
      {list}
    </div>
  );
}

function RunItem({ run }: { run: ScheduleRun }) {
  const duration = run.started_at && run.finished_at ? durationBetween(run.started_at, run.finished_at) : null;
  const artifacts = run.artifacts ?? [];
  return (
    <li className="rounded-card border border-border bg-surface p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <Link href={`/tasks/${run.id}`} className="line-clamp-1 text-[13px] font-medium text-fg hover:underline">
            {run.title}
          </Link>
          <p className="mt-0.5 text-[11.5px] text-muted">
            {run.started_at ? `Started ${formatDateTime(run.started_at)}` : `Queued ${formatDateTime(run.created_at)}`}
            {run.finished_at &&
              ` · Finished ${run.started_at && isSameDay(run.started_at, run.finished_at) ? formatTime(run.finished_at) : formatDateTime(run.finished_at)}`}
            {duration !== null && ` · ${formatDuration(duration)}`}
          </p>
        </div>
        <TaskStatusPill status={run.status} size="xs" />
      </div>
      {run.error && (
        <p className="mt-2 line-clamp-4 rounded-md border border-[color-mix(in_srgb,var(--danger)_25%,transparent)] bg-[color-mix(in_srgb,var(--danger)_5%,transparent)] px-2.5 py-1.5 text-xs break-words whitespace-pre-wrap text-danger">
          {run.error}
        </p>
      )}
      {artifacts.length > 0 && (
        <ul className="mt-2.5 divide-y divide-border rounded-md border border-border" aria-label="Files produced">
          {artifacts.map((a) => (
            <li key={a.id} className="flex items-center gap-2 py-1.5 pr-1.5 pl-2.5 text-xs">
              <FileKindIcon kind={a.kind} name={a.name} className="size-3.5 text-muted" />
              <span className="min-w-0 flex-1 truncate text-fg" title={a.path}>
                {a.name}
              </span>
              <span className="shrink-0 text-subtle tabular-nums">{formatBytes(a.size)}</span>
              <ViewFileButton source={{ artifactId: a.id }} name={a.name} />
              <IconButton
                size="xs"
                label={`Download ${a.name}`}
                icon={<Download />}
                onClick={() => void downloadFile({ artifactId: a.id }, a.name)}
              />
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function RunHistory({ job, runs, loading, error, onRetry }: { job: ScheduledJob; runs: ScheduleRun[] | undefined; loading: boolean; error: unknown; onRetry: () => void }) {
  if (runs === undefined) {
    if (loading)
      return (
        <div className="space-y-2.5">
          {Array.from({ length: 2 }).map((_, i) => (
            <Skeleton key={i} className="h-16 w-full rounded-card" />
          ))}
        </div>
      );
    return error ? <ErrorState compact error={error} onRetry={onRetry} /> : null;
  }
  if (runs.length === 0) {
    const text =
      job.kind === "notification"
        ? job.last_run_at
          ? `Reminders arrive as notifications — the last one was sent ${formatDateTime(job.last_run_at)}.`
          : "Reminders arrive as notifications; none has been sent yet."
        : isScheduleActive(job.status)
          ? job.status === "paused"
            ? "No runs yet. Resume the schedule to start running it."
            : `No runs yet — the first one is due ${formatDateTime(job.run_at, { weekday: true })}.`
          : "This schedule never ran.";
    return <p className="rounded-card border border-dashed border-border px-3 py-4 text-center text-xs text-muted">{text}</p>;
  }
  return (
    <ul className="space-y-2.5">
      {runs.map((r) => (
        <RunItem key={r.id} run={r} />
      ))}
    </ul>
  );
}

/** Right-hand detail for one schedule: actions, timing, instructions and run history (with files). */
export function ScheduleDrawer({
  id,
  initial,
  onClose,
  onEdit,
}: {
  id: string | null;
  /** The job from the list, shown while the detail loads. */
  initial?: ScheduledJob | null;
  onClose: () => void;
  onEdit: (job: ScheduledJob) => void;
}) {
  const detail = useSchedule(id);
  const agents = useAgentsMap();
  const job = detail.data?.job ?? initial ?? null;
  const notFound = detail.error instanceof ApiError && detail.error.isNotFound;

  let content: ReactNode;
  if (!job) {
    content = detail.isLoading ? (
      <div className="space-y-4">
        <Skeleton className="h-5 w-40" />
        <Skeleton className="h-28 w-full rounded-card" />
        <SkeletonText lines={4} />
      </div>
    ) : notFound ? (
      <EmptyState
        compact
        icon={<CalendarClock />}
        title="Schedule not found"
        description="It may have been deleted, or it belongs to someone else."
      />
    ) : (
      <ErrorState compact error={detail.error} onRetry={() => void detail.refetch()} />
    );
  } else {
    const agent = job.agent_id ? agents.get(job.agent_id) : undefined;
    content = (
      <div className="space-y-6">
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-[13px]">
            <ScheduleStatusBadge status={job.status} size="sm" />
            <ScheduleAgent job={job} agent={agent} size="xs" className="text-fg" />
          </div>
          <ScheduleActions job={job} onEdit={onEdit} onDeleted={onClose} variant="buttons" />
        </div>

        <DrawerSection title="Schedule" icon={<CalendarClock />}>
          <div className="rounded-card border border-border bg-surface p-3.5">
            <p className="flex items-start gap-2 text-[13.5px] font-medium text-fg">
              {job.cron ? <Repeat className="mt-0.5 size-3.5 shrink-0 text-muted" /> : <CalendarClock className="mt-0.5 size-3.5 shrink-0 text-muted" />}
              <span className="min-w-0">{scheduleSummary(job)}</span>
            </p>
            <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3 text-[13px]">
              <Item label="Next run">
                <NextRun job={job} />
              </Item>
              <Item label="Last run">
                <LastRun job={job} />
              </Item>
              <Item label="Time zone">
                <TimeZoneChip timeZone={job.timezone} />
              </Item>
              <Item label="Runs">
                <span className="tabular-nums">{job.run_count}</span>
              </Item>
              <Item label="Created">{formatDateTime(job.created_at)}</Item>
              {job.conversation_id && (
                <Item label="Set up in">
                  <Link href={`/chat/${job.conversation_id}`} className="text-fg underline-offset-2 hover:underline">
                    Conversation
                  </Link>
                </Item>
              )}
            </dl>
          </div>
          <UpcomingRuns job={job} />
        </DrawerSection>

        <DrawerSection title={job.kind === "notification" ? "Message" : "Instructions"} icon={<FileText />}>
          {job.instructions ? (
            <div className="rounded-card border border-border bg-surface px-4 py-3">
              <Markdown content={job.instructions} size="sm" />
            </div>
          ) : (
            <p className="text-[13px] text-muted">No instructions recorded.</p>
          )}
        </DrawerSection>

        <DrawerSection
          title="Run history"
          icon={<History />}
          aside={detail.data && detail.data.runs.length > 0 ? <span className="text-[11px] text-subtle">Latest {detail.data.runs.length}</span> : null}
        >
          <RunHistory
            job={job}
            runs={detail.data?.runs}
            loading={detail.isLoading}
            error={detail.error}
            onRetry={() => void detail.refetch()}
          />
        </DrawerSection>
      </div>
    );
  }

  return (
    <Drawer
      open={!!id}
      onOpenChange={(o) => !o && onClose()}
      width={560}
      title={job?.title ?? "Schedule"}
    >
      {content}
    </Drawer>
  );
}
