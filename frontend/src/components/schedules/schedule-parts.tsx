"use client";

import { Bell, Globe, Pause, Pencil, Play, Trash2, Zap } from "lucide-react";
import Link from "next/link";
import { TaskStatusPill } from "@/components/domain/status";
import { AgentAvatar } from "@/components/ui/avatar";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button, IconButton } from "@/components/ui/button";
import { confirm } from "@/components/ui/overlay";
import { useDeleteSchedule, useRunScheduleNow, useScheduleToggle } from "@/hooks/actions";
import { useBrowserTimeZone, useNow } from "@/hooks/use-browser";
import { formatDateTime, formatTimeZone, isPast, timeAgo, timeUntil } from "@/lib/format";
import { isScheduleActive, SCHEDULE_STATUS_LABEL } from "@/lib/schedules";
import type { Agent, ScheduledJob, ScheduleStatus } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useRole } from "@/providers/auth";

const STATUS_TONE: Record<ScheduleStatus, BadgeTone> = {
  scheduled: "accent",
  paused: "info",
  done: "success",
  cancelled: "outline",
  failed: "danger",
};

export function ScheduleStatusBadge({ status, size = "xs" }: { status: ScheduleStatus; size?: "xs" | "sm" | "md" }) {
  return (
    <Badge tone={STATUS_TONE[status] ?? "neutral"} size={size} dot>
      {SCHEDULE_STATUS_LABEL[status] ?? status}
    </Badge>
  );
}

export function TimeZoneChip({ timeZone, className }: { timeZone: string; className?: string }) {
  return (
    <span
      title={`Time zone: ${timeZone}`}
      className={cn(
        "inline-flex h-5 max-w-full shrink-0 items-center gap-1 rounded-full border border-border px-1.5 text-[11px] text-muted",
        className,
      )}
    >
      <Globe className="size-3 shrink-0" aria-hidden />
      <span className="truncate">{formatTimeZone(timeZone)}</span>
    </span>
  );
}

const AVATAR_BOX = { xs: "size-5 rounded-[5px] [&_svg]:size-3", sm: "size-6 rounded-md [&_svg]:size-3.5", md: "size-8 rounded-lg [&_svg]:size-4" };

/** Who runs it: the agent (avatar + name), or a bell for reminders. */
export function ScheduleAgent({
  job,
  agent,
  size = "sm",
  className,
}: {
  job: Pick<ScheduledJob, "kind" | "agent_name">;
  agent?: Agent;
  size?: "xs" | "sm" | "md";
  className?: string;
}) {
  if (job.kind === "notification") {
    return (
      <span className={cn("inline-flex min-w-0 items-center gap-2", className)}>
        <span className={cn("inline-flex shrink-0 items-center justify-center tint-accent", AVATAR_BOX[size])}>
          <Bell aria-hidden />
        </span>
        <span className="truncate">Reminder</span>
      </span>
    );
  }
  const name = agent?.name ?? job.agent_name ?? "Unknown agent";
  return (
    <span className={cn("inline-flex min-w-0 items-center gap-2", className)}>
      <AgentAvatar agent={agent} name={name} size={size} />
      <span className="truncate">{name}</span>
    </span>
  );
}

/** "in 14h" that stays current (re-renders with the shared clock). */
export function Countdown({ value }: { value: string }) {
  useNow();
  return <>{isPast(value) ? "due now" : timeUntil(value)}</>;
}

/** Next run in local time + relative; the job's own zone too when its wall clock differs. */
export function NextRun({ job, className }: { job: ScheduledJob; className?: string }) {
  const browserTz = useBrowserTimeZone();
  if (!isScheduleActive(job.status)) return <span className={cn("text-subtle", className)}>—</span>;
  const local = formatDateTime(job.run_at, { weekday: true });
  const zoned = formatDateTime(job.run_at, { weekday: true, timeZone: job.timezone });
  const showZoned = !!job.timezone && job.timezone !== browserTz && zoned !== local;
  const paused = job.status === "paused";
  return (
    <span className={cn("flex min-w-0 flex-col", className)}>
      <span className={cn("truncate", paused ? "text-muted" : "text-fg")}>{local}</span>
      <span className="truncate text-[11px] text-subtle">{paused ? "On hold while paused" : <Countdown value={job.run_at} />}</span>
      {showZoned && (
        <span className="truncate text-[11px] text-subtle" title={`In ${job.timezone}`}>
          {zoned} · {formatTimeZone(job.timezone)}
        </span>
      )}
    </span>
  );
}

/** Status of the latest run (links to its task) and when it ran. */
export function LastRun({ job, className }: { job: ScheduledJob; className?: string }) {
  const task = job.last_task;
  const when = job.last_run_at ?? task?.created_at ?? null;
  if (!task && !when) return <span className={cn("text-muted", className)}>Never</span>;
  return (
    <span className={cn("flex min-w-0 flex-col items-start gap-0.5", className)}>
      {task ? (
        <Link href={`/tasks/${task.id}`} onClick={(e) => e.stopPropagation()} title={`Open “${task.title}”`} className="rounded-full">
          <TaskStatusPill status={task.status} size="xs" />
        </Link>
      ) : (
        <span className="text-fg">Ran</span>
      )}
      <span className="text-[11px] text-subtle" title={formatDateTime(when)}>
        {timeAgo(when)}
      </span>
    </span>
  );
}

/** Run now · Pause/Resume · Edit · Delete (for members, on active schedules). */
export function ScheduleActions({
  job,
  onEdit,
  onDeleted,
  variant = "icons",
  className,
}: {
  job: ScheduledJob;
  onEdit: (job: ScheduledJob) => void;
  onDeleted?: () => void;
  variant?: "icons" | "buttons";
  className?: string;
}) {
  const { canWrite } = useRole();
  const run = useRunScheduleNow();
  const toggle = useScheduleToggle();
  const remove = useDeleteSchedule();
  if (!canWrite || !isScheduleActive(job.status)) return null;

  const busy = run.isPending || toggle.isPending || remove.isPending;
  const paused = job.status === "paused";
  const runNow = () => run.mutate(job);
  const flip = () => toggle.mutate({ job, action: paused ? "resume" : "pause" });
  const del = async () => {
    const ok = await confirm({
      title: "Delete this schedule?",
      description: `“${job.title}” won't run again. Past runs stay in its history.`,
      confirmLabel: "Delete schedule",
      cancelLabel: "Keep it",
      danger: true,
    });
    if (ok) remove.mutate(job, { onSuccess: () => onDeleted?.() });
  };

  if (variant === "buttons") {
    return (
      <div className={cn("flex flex-wrap items-center gap-2", className)}>
        <Button size="sm" variant="primary" icon={<Zap className="size-3.5" />} loading={run.isPending} disabled={busy} onClick={runNow}>
          Run now
        </Button>
        <Button
          size="sm"
          icon={paused ? <Play className="size-3.5" /> : <Pause className="size-3.5" />}
          loading={toggle.isPending}
          disabled={busy}
          onClick={flip}
        >
          {paused ? "Resume" : "Pause"}
        </Button>
        <Button size="sm" icon={<Pencil className="size-3.5" />} disabled={busy} onClick={() => onEdit(job)}>
          Edit
        </Button>
        <Button size="sm" variant="danger-outline" icon={<Trash2 className="size-3.5" />} loading={remove.isPending} disabled={busy} onClick={() => void del()}>
          Delete
        </Button>
      </div>
    );
  }
  return (
    <div className={cn("flex items-center gap-0.5", className)} onClick={(e) => e.stopPropagation()}>
      <IconButton label={`Run “${job.title}” now`} icon={<Zap />} loading={run.isPending} disabled={busy} onClick={runNow} />
      <IconButton
        label={paused ? `Resume “${job.title}”` : `Pause “${job.title}”`}
        icon={paused ? <Play /> : <Pause />}
        loading={toggle.isPending}
        disabled={busy}
        onClick={flip}
      />
      <IconButton label={`Edit “${job.title}”`} icon={<Pencil />} disabled={busy} onClick={() => onEdit(job)} />
      <IconButton
        label={`Delete “${job.title}”`}
        icon={<Trash2 />}
        variant="danger"
        loading={remove.isPending}
        disabled={busy}
        onClick={() => void del()}
      />
    </div>
  );
}
