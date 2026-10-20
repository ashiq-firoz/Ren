"use client";

import { Pause, Play, RotateCcw, Square } from "lucide-react";
import Link from "next/link";
import { isTaskActive, TaskStatusPill } from "@/components/domain/status";
import { AgentAvatar } from "@/components/ui/avatar";
import { IconButton } from "@/components/ui/button";
import { ProgressBar } from "@/components/ui/feedback";
import { useTaskAction } from "@/hooks/actions";
import { useAgentsMap } from "@/hooks/queries";
import { useNow } from "@/hooks/use-browser";
import { formatDuration } from "@/lib/format";
import type { Task } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useRole } from "@/providers/auth";

export function taskElapsed(task: Pick<Task, "started_at" | "finished_at">, now: number): number | null {
  if (!task.started_at) return null;
  const start = Date.parse(task.started_at);
  const end = task.finished_at ? Date.parse(task.finished_at) : now || start;
  return Math.max(0, (end - start) / 1000);
}

export function TaskActions({ task, size = "xs" }: { task: Task; size?: "xs" | "sm" }) {
  const action = useTaskAction();
  const { canWrite } = useRole();
  if (!canWrite) return null;
  const busy = action.isPending;
  const run = (a: "pause" | "resume" | "cancel" | "retry") => action.mutate({ id: task.id, action: a });
  return (
    <div className="flex items-center gap-0.5">
      {(task.status === "running" || task.status === "queued" || task.status === "waiting_approval") && (
        <IconButton size={size} label="Pause task" icon={<Pause />} disabled={busy} onClick={() => run("pause")} />
      )}
      {task.status === "paused" && (
        <IconButton size={size} label="Resume task" icon={<Play />} disabled={busy} onClick={() => run("resume")} />
      )}
      {isTaskActive(task.status) && (
        <IconButton size={size} label="Cancel task" icon={<Square />} disabled={busy} onClick={() => run("cancel")} />
      )}
      {(task.status === "failed" || task.status === "cancelled") && (
        <IconButton size={size} label="Retry task" icon={<RotateCcw />} disabled={busy} onClick={() => run("retry")} />
      )}
    </div>
  );
}

/** Live task summary: agent, title, status, progress, current step, elapsed time. */
export function TaskLiveCard({
  task,
  className,
  showActions = true,
  compact = false,
}: {
  task: Task;
  className?: string;
  showActions?: boolean;
  compact?: boolean;
}) {
  const agents = useAgentsMap();
  const agent = agents.get(task.agent_id);
  const active = isTaskActive(task.status);
  const now = useNow(task.status === "running");
  const elapsed = taskElapsed(task, now);

  return (
    <div className={cn("rounded-card border border-border bg-surface p-3", className)}>
      <div className="flex items-start gap-2.5">
        <AgentAvatar agent={agent} name={task.agent_name} color={agent?.color ?? task.agent_color} size="sm" className="mt-0.5" />
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            <Link href={`/tasks/${task.id}`} className="line-clamp-2 text-[13px] leading-snug font-medium text-fg hover:underline">
              {task.title}
            </Link>
            {showActions && <TaskActions task={task} />}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11.5px] text-muted">
            <span className="truncate">{task.agent_name}</span>
            <TaskStatusPill status={task.status} size="xs" />
            {elapsed !== null && <span className="tabular-nums text-subtle">{formatDuration(elapsed)}</span>}
          </div>
        </div>
      </div>
      {active && !compact && (
        <div className="mt-2.5 space-y-1.5 pl-[34px]">
          <ProgressBar
            value={task.progress}
            size="xs"
            indeterminate={task.status === "running" && (!task.progress || task.progress <= 0)}
            tone={task.status === "waiting_approval" ? "warning" : task.status === "paused" ? "info" : "accent"}
          />
          {task.current_step && <p className="line-clamp-2 text-[11.5px] text-muted">{task.current_step}</p>}
        </div>
      )}
    </div>
  );
}
