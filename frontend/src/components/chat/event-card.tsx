"use client";

import {
  ArrowUpRight,
  Ban,
  CheckCircle2,
  CircleSlash,
  Info,
  PauseCircle,
  ShieldAlert,
  ShieldCheck,
  ShieldX,
  Sparkles,
  XCircle,
} from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { ApprovalInline } from "@/components/domain/approval-card";
import { FileChip } from "@/components/files/file-link";
import { RiskBadge } from "@/components/domain/status";
import { ButtonLink } from "@/components/ui/button";
import { Markdown } from "@/components/ui/markdown";
import { useApprovals, useConversationTasks } from "@/hooks/queries";
import { useNow } from "@/hooks/use-browser";
import { formatDuration, formatTime } from "@/lib/format";
import type { ChatEvent, Message, Task } from "@/lib/types";
import { cn } from "@/lib/utils";
import { TaskActions, taskElapsed, TaskLiveCard } from "./task-live-card";

function useLiveTask(conversationId: string, taskId: string | undefined): Task | undefined {
  const { data } = useConversationTasks(conversationId);
  return taskId ? data?.items.find((t) => t.id === taskId) : undefined;
}

function fallbackTask(ev: ChatEvent, message: Message): Task {
  return {
    id: ev.task_id ?? message.task_id ?? message.id,
    title: ev.title ?? "Task",
    instructions: "",
    status: (ev.status as Task["status"]) ?? "queued",
    priority: "normal",
    agent_id: ev.agent_id ?? message.agent_id ?? "",
    agent_name: ev.agent_name ?? "Agent",
    agent_color: "",
    conversation_id: message.conversation_id,
    parent_id: null,
    skill_id: null,
    progress: 0,
    current_step: null,
    result: null,
    error: null,
    steps: 0,
    tokens: 0,
    created_by: null,
    created_at: message.created_at,
    started_at: null,
    finished_at: null,
    children_count: 0,
    artifacts: [],
    schedule_id: null,
  };
}

function Shell({
  icon,
  tone,
  title,
  time,
  children,
  aside,
}: {
  icon: ReactNode;
  tone: "neutral" | "success" | "danger" | "warning" | "accent";
  title: ReactNode;
  time?: string;
  children?: ReactNode;
  aside?: ReactNode;
}) {
  return (
    <div className="flex gap-3">
      <span
        className={cn(
          "mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full border [&_svg]:size-3.5",
          tone === "neutral" && "border-border bg-surface-2 text-muted",
          tone === "success" && "tint-success border-transparent",
          tone === "danger" && "tint-danger border-transparent",
          tone === "warning" && "tint-warning border-transparent",
          tone === "accent" && "tint-accent border-transparent",
        )}
      >
        {icon}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex min-h-7 items-center justify-between gap-2">
          <div className="min-w-0 text-[13px] text-muted">{title}</div>
          <div className="flex shrink-0 items-center gap-2">
            {aside}
            {time && <span className="text-[11px] text-subtle">{time}</span>}
          </div>
        </div>
        {children && <div className="mt-1.5">{children}</div>}
      </div>
    </div>
  );
}

function TaskCreated({ ev, message }: { ev: ChatEvent; message: Message }) {
  const live = useLiveTask(message.conversation_id, ev.task_id ?? message.task_id ?? undefined);
  const task = live ?? fallbackTask(ev, message);
  return (
    <Shell
      icon={<ArrowUpRight />}
      tone="accent"
      title={
        <>
          Ren delegated to <span className="font-medium text-fg">{task.agent_name || ev.agent_name}</span>
        </>
      }
      time={formatTime(message.created_at)}
      aside={
        task.id && live ? (
          <Link href={`/tasks/${task.id}`} className="text-xs font-medium text-muted hover:text-fg">
            View
          </Link>
        ) : null
      }
    >
      <TaskLiveCard task={task} showActions={!!live} />
    </Shell>
  );
}

function TaskFinished({ ev, message }: { ev: ChatEvent; message: Message }) {
  const live = useLiveTask(message.conversation_id, ev.task_id ?? message.task_id ?? undefined);
  const now = useNow(false);
  const taskId = ev.task_id ?? message.task_id;
  const elapsed = live ? taskElapsed(live, now) : null;
  const title = live?.title ?? ev.title ?? "Task";
  const agent = live?.agent_name ?? ev.agent_name ?? "Agent";

  if (ev.kind === "task_completed") {
    const summary = ev.summary ?? live?.result ?? null;
    const files = live?.artifacts ?? [];
    return (
      <Shell
        icon={<CheckCircle2 />}
        tone="success"
        title={
          <>
            <span className="font-medium text-fg">{agent}</span> completed <span className="text-fg">“{title}”</span>
            {elapsed !== null && <span className="text-subtle"> · {formatDuration(elapsed)}</span>}
          </>
        }
        time={formatTime(message.created_at)}
      >
        {summary && (
          <div className="rounded-card border border-border bg-surface px-3.5 py-3">
            <div className="line-clamp-6">
              <Markdown content={summary} size="sm" />
            </div>
          </div>
        )}
        {files.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5" aria-label="Files produced">
            {files.slice(0, 4).map((a) => (
              <FileChip key={a.id} source={{ artifactId: a.id }} name={a.name} kind={a.kind} size={a.size} />
            ))}
            {files.length > 4 && taskId && (
              <Link
                href={`/tasks/${taskId}?tab=artifacts`}
                className="inline-flex h-8 items-center rounded-lg px-2 text-xs font-medium text-muted hover:text-fg"
              >
                +{files.length - 4} more
              </Link>
            )}
          </div>
        )}
        {taskId && (
          <div className="mt-2">
            <ButtonLink href={`/tasks/${taskId}?tab=result`} size="xs" variant="secondary">
              View result
            </ButtonLink>
          </div>
        )}
      </Shell>
    );
  }

  if (ev.kind === "task_failed") {
    return (
      <Shell
        icon={<XCircle />}
        tone="danger"
        title={
          <>
            <span className="font-medium text-fg">{agent}</span> couldn&apos;t finish <span className="text-fg">“{title}”</span>
          </>
        }
        time={formatTime(message.created_at)}
        aside={live ? <TaskActions task={live} /> : null}
      >
        {(ev.summary || live?.error) && (
          <p className="rounded-card border border-[color-mix(in_srgb,var(--danger)_25%,transparent)] bg-[color-mix(in_srgb,var(--danger)_5%,transparent)] px-3 py-2 text-xs text-danger">
            {ev.summary ?? live?.error}
          </p>
        )}
        {taskId && (
          <Link href={`/tasks/${taskId}`} className="mt-1.5 inline-block text-xs font-medium text-muted hover:text-fg">
            Open task →
          </Link>
        )}
      </Shell>
    );
  }

  const paused = ev.kind === "task_paused";
  return (
    <Shell
      icon={paused ? <PauseCircle /> : <CircleSlash />}
      tone="neutral"
      title={
        <>
          <span className="text-fg">“{title}”</span> was {paused ? "paused" : "cancelled"}
          {ev.summary ? <span className="text-subtle"> · {ev.summary}</span> : null}
        </>
      }
      time={formatTime(message.created_at)}
      aside={live ? <TaskActions task={live} /> : null}
    />
  );
}

function ApprovalRequested({ ev, message }: { ev: ChatEvent; message: Message }) {
  const pending = useApprovals("pending");
  const approval = ev.approval_id ? pending.data?.items.find((a) => a.id === ev.approval_id) : undefined;
  const resolved = !approval && (ev.decision || (pending.data && ev.approval_id));
  return (
    <Shell
      icon={<ShieldAlert />}
      tone="warning"
      title={
        <>
          <span className="font-medium text-fg">{ev.agent_name ?? approval?.agent_name ?? "An agent"}</span> needs approval
          {ev.title && <span className="text-subtle"> · {ev.title}</span>}
        </>
      }
      time={formatTime(message.created_at)}
    >
      <div className="rounded-card border border-[color-mix(in_srgb,var(--warning)_30%,transparent)] bg-surface p-3.5">
        {approval ? (
          <ApprovalInline approval={approval} />
        ) : (
          <div className="space-y-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <RiskBadge risk={ev.risk} size="xs" />
              <span className="text-[13px] font-medium text-fg">{ev.tool ?? "Tool call"}</span>
            </div>
            {ev.summary && <p className="text-xs text-muted">{ev.summary}</p>}
            <p className="text-xs text-subtle">
              {ev.decision
                ? `This request was ${ev.decision}.`
                : resolved
                  ? "This request has been resolved."
                  : "Loading request…"}
            </p>
          </div>
        )}
      </div>
    </Shell>
  );
}

function ApprovalResolved({ ev, message }: { ev: ChatEvent; message: Message }) {
  const approved = ev.decision === "approved";
  const expired = ev.decision === "expired";
  return (
    <Shell
      icon={approved ? <ShieldCheck /> : expired ? <Ban /> : <ShieldX />}
      tone={approved ? "success" : expired ? "neutral" : "danger"}
      title={
        <>
          {ev.tool ? <span className="text-fg">{ev.tool}</span> : "Request"} was{" "}
          <span className={cn("font-medium", approved ? "text-success" : expired ? "text-muted" : "text-danger")}>
            {ev.decision ?? "resolved"}
          </span>
          {ev.summary && <span className="text-subtle"> · {ev.summary}</span>}
        </>
      }
      time={formatTime(message.created_at)}
    />
  );
}

export function EventCard({ message }: { message: Message }) {
  const ev = message.metadata?.event;
  if (!ev) {
    return (
      <Shell icon={<Info />} tone="neutral" title={message.content || "Update"} time={formatTime(message.created_at)} />
    );
  }
  switch (ev.kind) {
    case "task_created":
      return <TaskCreated ev={ev} message={message} />;
    case "task_completed":
    case "task_failed":
    case "task_cancelled":
    case "task_paused":
      return <TaskFinished ev={ev} message={message} />;
    case "approval_requested":
      return <ApprovalRequested ev={ev} message={message} />;
    case "approval_resolved":
      return <ApprovalResolved ev={ev} message={message} />;
    case "skill_started":
      return (
        <Shell
          icon={<Sparkles />}
          tone="accent"
          title={
            <>
              Running skill <span className="font-medium text-fg">{ev.title ?? "skill"}</span>
              {ev.agent_name && <span className="text-subtle"> · {ev.agent_name}</span>}
            </>
          }
          time={formatTime(message.created_at)}
        />
      );
    case "info":
    default:
      return (
        <Shell
          icon={<Info />}
          tone="neutral"
          title={<span>{ev.summary ?? ev.title ?? message.content}</span>}
          time={formatTime(message.created_at)}
        />
      );
  }
}
