"use client";

import {
  ArrowLeft,
  CalendarClock,
  Download,
  FileBox,
  GitBranch,
  MessageSquare,
  Pause,
  Play,
  RotateCcw,
  Send,
  ShieldAlert,
  Square,
} from "lucide-react";
import Link from "next/link";
import { useParams, usePathname, useRouter, useSearchParams } from "next/navigation";
import { useRef, useState } from "react";
import { ApprovalInline } from "@/components/domain/approval-card";
import { isTaskActive, isTaskTerminal, PriorityBadge, TaskStatusPill } from "@/components/domain/status";
import { TaskTable } from "@/components/domain/task-table";
import { downloadArtifact, TaskTimeline } from "@/components/domain/task-timeline";
import { FileKindIcon } from "@/components/files/file-icon";
import { ViewFileButton } from "@/components/files/file-link";
import { useFileClick } from "@/components/files/file-viewer-context";
import { MobileNavButton } from "@/components/shell/page-header";
import { NotificationBell } from "@/components/shell/notification-bell";
import { PageContainer } from "@/components/shell/page-header";
import { AgentAvatar } from "@/components/ui/avatar";
import { Button, ButtonLink } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, ErrorState, ProgressBar, Skeleton, SkeletonText } from "@/components/ui/feedback";
import { KeyValue } from "@/components/ui/key-value";
import { Markdown } from "@/components/ui/markdown";
import { Table, TableContainer, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { Tabs } from "@/components/ui/tabs";
import { useTaskAction, useTaskMessage } from "@/hooks/actions";
import { useAgentsMap, useTask } from "@/hooks/queries";
import { useNow } from "@/hooks/use-browser";
import { ApiError } from "@/lib/api";
import { FILE_KIND_LABEL, viewerHref } from "@/lib/files";
import { formatBytes, formatDateTime, formatDuration, formatNumber, timeAgo } from "@/lib/format";
import type { TaskDetail } from "@/lib/types";
import { useRole } from "@/providers/auth";

type Tab = "timeline" | "result" | "artifacts" | "subtasks" | "instructions";
const TABS: Tab[] = ["timeline", "result", "artifacts", "subtasks", "instructions"];

function MessageAgent({ task, inputRef }: { task: TaskDetail; inputRef: React.RefObject<HTMLTextAreaElement | null> }) {
  const [text, setText] = useState("");
  const send = useTaskMessage(task.id);
  const active = isTaskActive(task.status);
  const submit = () => {
    const content = text.trim();
    if (!content) return;
    send.mutate(content, { onSuccess: () => setText("") });
  };
  return (
    <div className="mt-2 rounded-card border border-border bg-surface p-2">
      <div className="flex items-end gap-2">
        <textarea
          ref={inputRef}
          value={text}
          rows={1}
          onChange={(e) => {
            setText(e.target.value);
            e.target.style.height = "auto";
            e.target.style.height = `${Math.min(e.target.scrollHeight, 160)}px`;
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder={
            active
              ? `Message ${task.agent_name} — delivered before its next step…`
              : `${task.agent_name} isn't running. Messages are kept in the task's inbox.`
          }
          aria-label={`Message ${task.agent_name}`}
          className="max-h-40 min-h-9 flex-1 resize-none bg-transparent px-2 py-2 text-[13.5px] text-fg outline-none placeholder:text-subtle"
        />
        <Button variant="primary" size="sm" icon={<Send className="size-3.5" />} loading={send.isPending} disabled={!text.trim()} onClick={submit}>
          Send
        </Button>
      </div>
    </div>
  );
}

export default function TaskDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const { canWrite } = useRole();
  const { data: task, isLoading, error, refetch } = useTask(id);
  const agents = useAgentsMap();
  const action = useTaskAction();
  const messageRef = useRef<HTMLTextAreaElement | null>(null);
  const now = useNow(task?.status === "running");
  const fileClick = useFileClick();

  const tabParam = params.get("tab") as Tab | null;
  const tab: Tab = tabParam && TABS.includes(tabParam) ? tabParam : "timeline";
  const setTab = (t: Tab) => router.replace(`${pathname}${t === "timeline" ? "" : `?tab=${t}`}`, { scroll: false });

  if (isLoading) {
    return (
      <PageContainer>
        <Skeleton className="h-4 w-24" />
        <Skeleton className="mt-4 h-9 w-2/3" />
        <Skeleton className="mt-3 h-4 w-1/3" />
        <Skeleton className="mt-8 h-24 w-full rounded-card" />
        <SkeletonText lines={6} className="mt-8" />
      </PageContainer>
    );
  }

  if (error || !task) {
    const notFound = error instanceof ApiError && (error.status === 404 || error.status === 403);
    return (
      <PageContainer>
        <Card>
          {notFound ? (
            <EmptyState
              title="Task not found"
              description="It may belong to another workspace, or you don't have access to it."
              action={<ButtonLink href="/tasks">Back to tasks</ButtonLink>}
            />
          ) : (
            <ErrorState error={error} onRetry={() => refetch()} />
          )}
        </Card>
      </PageContainer>
    );
  }

  const agent = agents.get(task.agent_id);
  const elapsed = task.started_at
    ? Math.max(0, ((task.finished_at ? Date.parse(task.finished_at) : now || Date.parse(task.started_at)) - Date.parse(task.started_at)) / 1000)
    : null;
  const pendingApprovals = (task.approvals ?? []).filter((a) => a.status === "pending");
  const busy = action.isPending;
  const run = (a: "pause" | "resume" | "cancel" | "retry") => action.mutate({ id: task.id, action: a });

  return (
    <PageContainer>
      <div className="mb-5 flex items-center justify-between gap-3">
        <div className="flex items-center gap-1">
          <MobileNavButton className="-ml-1.5" />
          <Link href="/tasks" className="inline-flex items-center gap-1.5 text-[13px] text-muted hover:text-fg">
            <ArrowLeft className="size-3.5" /> Tasks
          </Link>
          {task.parent_id && (
            <>
              <span className="mx-1.5 text-subtle">/</span>
              <Link href={`/tasks/${task.parent_id}`} className="inline-flex items-center gap-1 text-[13px] text-muted hover:text-fg">
                <GitBranch className="size-3.5" /> Parent task
              </Link>
            </>
          )}
        </div>
        <NotificationBell />
      </div>

      <header className="mb-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <TaskStatusPill status={task.status} size="md" />
              <PriorityBadge priority={task.priority} />
              {task.conversation_id && (
                <Link
                  href={`/chat/${task.conversation_id}`}
                  className="inline-flex items-center gap-1 rounded-full border border-border px-2 py-0.5 text-[11.5px] text-muted hover:text-fg"
                >
                  <MessageSquare className="size-3" /> From conversation
                </Link>
              )}
              {task.schedule_id && (
                <Link
                  href={`/schedules?id=${encodeURIComponent(task.schedule_id)}`}
                  className="inline-flex items-center gap-1 rounded-full border border-border px-2 py-0.5 text-[11.5px] text-muted hover:text-fg"
                >
                  <CalendarClock className="size-3" /> From schedule
                </Link>
              )}
            </div>
            <h1 className="font-serif-display mt-2 text-[30px] leading-[1.12] text-fg sm:text-[34px]">{task.title}</h1>
            <div className="mt-2 flex items-center gap-2 text-[13px] text-muted">
              <AgentAvatar agent={agent} name={task.agent_name} color={agent?.color ?? task.agent_color} size="sm" />
              <Link href={`/agents/${task.agent_id}`} className="font-medium text-fg hover:underline">
                {task.agent_name}
              </Link>
              {agent?.role && <span>· {agent.role}</span>}
            </div>
          </div>
          {canWrite && (
            <div className="flex flex-wrap items-center gap-2">
              {isTaskActive(task.status) && (
                <Button icon={<MessageSquare className="size-4" />} onClick={() => messageRef.current?.focus()}>
                  Message agent
                </Button>
              )}
              {(task.status === "running" || task.status === "queued" || task.status === "waiting_approval") && (
                <Button icon={<Pause className="size-4" />} disabled={busy} onClick={() => run("pause")}>
                  Pause
                </Button>
              )}
              {task.status === "paused" && (
                <Button icon={<Play className="size-4" />} disabled={busy} onClick={() => run("resume")}>
                  Resume
                </Button>
              )}
              {isTaskActive(task.status) && (
                <Button variant="danger-outline" icon={<Square className="size-3.5" />} disabled={busy} onClick={() => run("cancel")}>
                  Cancel
                </Button>
              )}
              {(task.status === "failed" || task.status === "cancelled") && (
                <Button variant="primary" icon={<RotateCcw className="size-4" />} disabled={busy} onClick={() => run("retry")}>
                  Retry
                </Button>
              )}
            </div>
          )}
        </div>

        <Card className="mt-5 p-4">
          <div className="mb-4 flex items-center gap-3">
            <ProgressBar
              value={task.status === "completed" ? 100 : task.progress}
              size="sm"
              indeterminate={task.status === "running" && !task.progress}
              tone={task.status === "failed" ? "danger" : task.status === "completed" ? "success" : task.status === "waiting_approval" ? "warning" : "accent"}
            />
            <span className="w-10 text-right text-[13px] font-medium text-fg tabular-nums">
              {Math.round(task.status === "completed" ? 100 : task.progress)}%
            </span>
          </div>
          {task.current_step && isTaskActive(task.status) && (
            <p className="-mt-2 mb-4 text-[13px] text-muted">
              <span className="text-subtle">Now:</span> {task.current_step}
            </p>
          )}
          <KeyValue
            columns={4}
            items={[
              { label: "Created", value: <span title={formatDateTime(task.created_at)}>{timeAgo(task.created_at)}</span> },
              { label: "Started", value: task.started_at ? formatDateTime(task.started_at) : "Not yet" },
              { label: isTaskTerminal(task.status) ? "Finished" : "Running for", value: isTaskTerminal(task.status) ? formatDateTime(task.finished_at) : formatDuration(elapsed) },
              { label: "Duration", value: formatDuration(elapsed), hidden: !isTaskTerminal(task.status) },
              { label: "Steps", value: formatNumber(task.steps) },
              { label: "Tokens", value: formatNumber(task.tokens) },
              { label: "Sub-tasks", value: formatNumber(task.children_count ?? task.children?.length ?? 0) },
              { label: "Artifacts", value: formatNumber(task.artifacts?.length ?? 0) },
            ]}
          />
        </Card>
      </header>

      {pendingApprovals.length > 0 && (
        <div className="mb-6 space-y-3">
          {pendingApprovals.map((a) => (
            <div key={a.id} className="rounded-card border border-[color-mix(in_srgb,var(--warning)_35%,transparent)] bg-surface p-4">
              <p className="mb-3 flex items-center gap-2 text-[13px] font-medium text-fg">
                <ShieldAlert className="size-4 text-warning" /> This task is waiting for your approval
              </p>
              <ApprovalInline approval={a} />
            </div>
          ))}
        </div>
      )}

      <Tabs
        aria-label="Task sections"
        idPrefix="task-tab"
        value={tab}
        onValueChange={setTab}
        className="mb-5"
        items={[
          { value: "timeline", label: "Timeline", count: task.events?.length ?? 0 },
          { value: "result", label: "Result" },
          { value: "artifacts", label: "Artifacts", count: task.artifacts?.length ?? 0 },
          { value: "subtasks", label: "Sub-tasks", count: task.children?.length ?? 0 },
          { value: "instructions", label: "Instructions" },
        ]}
      />

      <div role="tabpanel" aria-labelledby={`task-tab-${tab}`}>
        {tab === "timeline" && (
          <div>
            <TaskTimeline events={task.events ?? []} />
            {canWrite && !isTaskTerminal(task.status) && <MessageAgent task={task} inputRef={messageRef} />}
          </div>
        )}

        {tab === "result" &&
          (task.result ? (
            <Card className="px-5 py-5 sm:px-7">
              <Markdown content={task.result} />
            </Card>
          ) : task.error ? (
            <Card className="p-5">
              <p className="mb-2 text-[13px] font-medium text-danger">The task failed</p>
              <pre className="overflow-auto rounded-lg bg-[var(--code-bg)] p-3 font-mono text-xs whitespace-pre-wrap text-danger">{task.error}</pre>
            </Card>
          ) : (
            <Card>
              <EmptyState
                compact
                title={isTaskActive(task.status) ? "Still working" : "No result"}
                description={isTaskActive(task.status) ? `${task.agent_name} will post the result here when it's done.` : "This task finished without a written result."}
              />
            </Card>
          ))}

        {tab === "artifacts" &&
          ((task.artifacts ?? []).length === 0 ? (
            <Card>
              <EmptyState compact icon={<FileBox />} title="No artifacts" description="Files the agent produces (reports, spreadsheets, charts) appear here." />
            </Card>
          ) : (
            <Card className="overflow-hidden">
              <TableContainer>
                <Table>
                  <THead>
                    <tr>
                      <TH>Name</TH>
                      <TH>Type</TH>
                      <TH className="text-right">Size</TH>
                      <TH>Created</TH>
                      <TH className="text-right">
                        <span className="sr-only">Actions</span>
                      </TH>
                    </tr>
                  </THead>
                  <TBody>
                    {task.artifacts.map((a) => (
                      <TR key={a.id}>
                        <TD className="max-w-[360px]">
                          <Link
                            href={viewerHref({ artifactId: a.id })}
                            prefetch={false}
                            onClick={fileClick({ artifactId: a.id })}
                            className="flex min-w-0 items-center gap-2 font-medium text-fg hover:underline"
                          >
                            <FileKindIcon kind={a.kind} name={a.name} className="size-4 text-muted" />
                            <span className="truncate">{a.name}</span>
                          </Link>
                        </TD>
                        <TD className="text-muted" title={a.mime}>
                          {a.kind ? FILE_KIND_LABEL[a.kind] ?? a.mime : a.mime}
                        </TD>
                        <TD className="text-right text-muted tabular-nums">{formatBytes(a.size)}</TD>
                        <TD className="text-muted" title={formatDateTime(a.created_at)}>
                          {timeAgo(a.created_at)}
                        </TD>
                        <TD className="text-right">
                          <div className="flex justify-end gap-1.5">
                            <ViewFileButton source={{ artifactId: a.id }} name={a.name} />
                            <Button size="xs" variant="secondary" icon={<Download className="size-3" />} onClick={() => void downloadArtifact(a)}>
                              Download
                            </Button>
                          </div>
                        </TD>
                      </TR>
                    ))}
                  </TBody>
                </Table>
              </TableContainer>
            </Card>
          ))}

        {tab === "subtasks" &&
          ((task.children ?? []).length === 0 ? (
            <Card>
              <EmptyState compact icon={<GitBranch />} title="No sub-tasks" description="When this agent delegates part of the work, the sub-tasks show up here." />
            </Card>
          ) : (
            <Card className="overflow-hidden">
              <TaskTable tasks={task.children} />
            </Card>
          ))}

        {tab === "instructions" && (
          <Card className="px-5 py-5 sm:px-7">
            {task.instructions ? <Markdown content={task.instructions} /> : <p className="text-[13px] text-muted">No instructions recorded.</p>}
          </Card>
        )}
      </div>
    </PageContainer>
  );
}
