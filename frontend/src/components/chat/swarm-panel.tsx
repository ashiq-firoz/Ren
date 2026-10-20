"use client";

import { Bot, CheckCircle2, CircleSlash, ShieldAlert, X, XCircle } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { ApprovalInline } from "@/components/domain/approval-card";
import { isTaskActive, isTaskTerminal } from "@/components/domain/status";
import { IconButton } from "@/components/ui/button";
import { ErrorState, Skeleton } from "@/components/ui/feedback";
import { stripMarkdown } from "@/components/ui/markdown";
import { useApprovals, useConversationTasks } from "@/hooks/queries";
import { formatDuration, timeAgo } from "@/lib/format";
import type { Task, TaskStatus } from "@/lib/types";
import { TaskLiveCard, taskElapsed } from "./task-live-card";

const ACTIVE_ORDER: Record<TaskStatus, number> = {
  waiting_approval: 0,
  running: 1,
  queued: 2,
  paused: 3,
  completed: 9,
  failed: 9,
  cancelled: 9,
};

function SectionTitle({ children, count }: { children: ReactNode; count?: number }) {
  return (
    <h3 className="mb-2 flex items-center gap-2 text-[11px] font-medium tracking-[0.06em] text-subtle uppercase">
      {children}
      {count !== undefined && count > 0 && (
        <span className="rounded-full bg-surface-3 px-1.5 py-px text-[10px] font-semibold text-muted">{count}</span>
      )}
    </h3>
  );
}

function CompletedRow({ task }: { task: Task }) {
  const icon =
    task.status === "completed" ? (
      <CheckCircle2 className="size-3.5 text-success" />
    ) : task.status === "failed" ? (
      <XCircle className="size-3.5 text-danger" />
    ) : (
      <CircleSlash className="size-3.5 text-subtle" />
    );
  const elapsed = taskElapsed(task, 0);
  const preview = task.status === "failed" ? task.error : task.result ? stripMarkdown(task.result) : null;
  return (
    <Link
      href={`/tasks/${task.id}${task.status === "completed" ? "?tab=result" : ""}`}
      className="block rounded-card border border-border bg-surface p-3 transition-colors hover:border-border-strong"
    >
      <div className="flex items-start gap-2">
        <span className="mt-0.5">{icon}</span>
        <div className="min-w-0 flex-1">
          <p className="line-clamp-1 text-[13px] font-medium text-fg">{task.title}</p>
          <p className="mt-0.5 text-[11.5px] text-muted">
            {task.agent_name}
            {elapsed !== null && ` · ${formatDuration(elapsed)}`}
            {task.finished_at && ` · ${timeAgo(task.finished_at)}`}
          </p>
          {preview && <p className="mt-1.5 line-clamp-2 text-xs text-muted">{preview}</p>}
        </div>
      </div>
    </Link>
  );
}

/** Right-hand "Swarm activity" for one conversation: live tasks, approvals, recent results. */
export function SwarmPanel({ conversationId, onClose }: { conversationId: string | null; onClose?: () => void }) {
  const tasks = useConversationTasks(conversationId);
  const approvals = useApprovals("pending");
  const items = tasks.data?.items ?? [];
  const active = items
    .filter((t) => isTaskActive(t.status))
    .sort((a, b) => ACTIVE_ORDER[a.status] - ACTIVE_ORDER[b.status] || b.created_at.localeCompare(a.created_at));
  const done = items
    .filter((t) => isTaskTerminal(t.status))
    .sort((a, b) => (b.finished_at ?? b.created_at).localeCompare(a.finished_at ?? a.created_at))
    .slice(0, 6);
  const taskIds = new Set(items.map((t) => t.id));
  const pending = (approvals.data?.items ?? []).filter(
    (a) => a.status === "pending" && (a.conversation_id === conversationId || (a.task_id && taskIds.has(a.task_id))),
  );

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-14 shrink-0 items-center justify-between border-b border-border px-4">
        <div className="flex items-center gap-2">
          <h2 className="text-[13.5px] font-semibold text-fg">Swarm activity</h2>
          {active.length > 0 && (
            <span className="inline-flex items-center gap-1.5 text-xs text-muted">
              <span className="size-1.5 animate-pulse-dot rounded-full bg-accent text-accent" />
              {active.length} working
            </span>
          )}
        </div>
        {onClose && <IconButton label="Close swarm panel" icon={<X />} onClick={onClose} />}
      </div>
      <div className="min-h-0 flex-1 space-y-6 overflow-y-auto p-4">
        {!conversationId ? (
          <div className="py-10 text-center">
            <div className="mx-auto mb-3 flex size-10 items-center justify-center rounded-xl border border-border bg-surface-2 text-muted">
              <Bot className="size-5" />
            </div>
            <p className="font-serif-display text-xl text-fg">The swarm is standing by</p>
            <p className="mt-1 text-xs leading-relaxed text-muted">
              When Ren delegates work, agents appear here with live progress. You can pause, cancel or approve their actions.
            </p>
          </div>
        ) : tasks.isLoading ? (
          <div className="space-y-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-20 w-full rounded-card" />
            ))}
          </div>
        ) : tasks.error ? (
          <ErrorState compact error={tasks.error} onRetry={() => tasks.refetch()} />
        ) : (
          <>
            {pending.length > 0 && (
              <section>
                <SectionTitle count={pending.length}>
                  <ShieldAlert className="size-3.5 text-warning" /> Needs your approval
                </SectionTitle>
                <div className="space-y-2.5">
                  {pending.map((a) => (
                    <div
                      key={a.id}
                      className="rounded-card border border-[color-mix(in_srgb,var(--warning)_30%,transparent)] bg-surface p-3"
                    >
                      <ApprovalInline approval={a} />
                    </div>
                  ))}
                </div>
              </section>
            )}
            <section>
              <SectionTitle count={active.length}>Working now</SectionTitle>
              {active.length === 0 ? (
                <p className="rounded-card border border-dashed border-border px-3 py-4 text-center text-xs text-muted">
                  No agents are working on this conversation right now.
                </p>
              ) : (
                <div className="space-y-2.5">
                  {active.map((t) => (
                    <TaskLiveCard key={t.id} task={t} />
                  ))}
                </div>
              )}
            </section>
            {done.length > 0 && (
              <section>
                <SectionTitle>Recently completed</SectionTitle>
                <div className="space-y-2">
                  {done.map((t) => (
                    <CompletedRow key={t.id} task={t} />
                  ))}
                </div>
              </section>
            )}
          </>
        )}
      </div>
      {conversationId && items.length > 0 && (
        <div className="shrink-0 border-t border-border px-4 py-2.5 text-right">
          <Link href={`/tasks?conversation_id=${conversationId}`} className="text-xs font-medium text-muted hover:text-fg">
            All tasks for this conversation →
          </Link>
        </div>
      )}
    </div>
  );
}
