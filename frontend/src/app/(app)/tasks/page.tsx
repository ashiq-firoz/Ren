"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, ChevronLeft, ChevronRight, ListChecks, Plus, Search, X } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { TaskTable } from "@/components/domain/task-table";
import { PageContainer, PageHeader } from "@/components/shell/page-header";
import { Badge } from "@/components/ui/badge";
import { Button, ButtonLink } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, ErrorState, SkeletonRows } from "@/components/ui/feedback";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { Dialog } from "@/components/ui/overlay";
import { Tabs } from "@/components/ui/tabs";
import { useAgents, useSchedules, useTasks } from "@/hooks/queries";
import { useDebounced } from "@/hooks/use-browser";
import { api } from "@/lib/api";
import { upsertTask } from "@/lib/cache";
import { toast } from "@/lib/toast";
import type { Priority, Task, TaskStatus } from "@/lib/types";
import { useRole } from "@/providers/auth";

const PAGE_SIZE = 50;

type StatusTab = "all" | "running" | "waiting_approval" | "completed" | "failed";

const STATUS_TABS: { value: StatusTab; label: string }[] = [
  { value: "all", label: "All" },
  { value: "running", label: "Running" },
  { value: "waiting_approval", label: "Waiting approval" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
];

function NewTaskDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient();
  const router = useRouter();
  const agents = useAgents();
  const options = (agents.data?.items ?? []).filter((a) => a.enabled && !a.is_orchestrator);
  const [agentId, setAgentId] = useState("");
  const [title, setTitle] = useState("");
  const [instructions, setInstructions] = useState("");
  const [priority, setPriority] = useState<Priority>("normal");
  const chosen = agentId || options[0]?.id || "";

  const create = useMutation({
    mutationFn: () =>
      api.post<Task>("/tasks", { agent_id: chosen, title: title.trim(), instructions: instructions.trim(), priority }),
    onSuccess: (task) => {
      upsertTask(qc, task);
      toast.success("Task assigned", { description: `${task.agent_name} is on it.` });
      onOpenChange(false);
      setTitle("");
      setInstructions("");
      router.push(`/tasks/${task.id}`);
    },
    meta: { errorTitle: "Couldn't create the task" },
  });

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Assign a task"
      description="Give work directly to a specialist. It runs in the background; you'll be notified when it finishes."
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={create.isPending}
            disabled={!chosen || !title.trim() || !instructions.trim()}
            onClick={() => create.mutate()}
          >
            Assign task
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-[1fr_160px]">
          <Field label="Agent" required>
            <Select value={chosen} onChange={(e) => setAgentId(e.target.value)} disabled={options.length === 0}>
              {options.length === 0 && <option value="">No agents available</option>}
              {options.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name} — {a.role}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Priority">
            <Select
              value={priority}
              onChange={(e) => setPriority(e.target.value as Priority)}
              options={[
                { value: "low", label: "Low" },
                { value: "normal", label: "Normal" },
                { value: "high", label: "High" },
                { value: "urgent", label: "Urgent" },
              ]}
            />
          </Field>
        </div>
        <Field label="Title" required>
          <Input data-autofocus value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Competitive pricing scan" />
        </Field>
        <Field label="Instructions" required hint="Be specific about the deliverable, sources and constraints.">
          <Textarea
            autosize
            minRows={5}
            maxRows={14}
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            placeholder="Compare our list prices with the top three competitors and produce a table plus a short recommendation."
          />
        </Field>
      </div>
    </Dialog>
  );
}

export default function TasksPage() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const { canWrite } = useRole();
  const agents = useAgents();
  const [newOpen, setNewOpen] = useState(false);

  // Scheduled work moved to its own page; keep old `?view=scheduled` links working.
  const legacyScheduledView = params.get("view") === "scheduled";
  useEffect(() => {
    if (legacyScheduledView) router.replace("/schedules");
  }, [legacyScheduledView, router]);
  const schedules = useSchedules({ status: "active" });
  const scheduledCount = schedules.data?.items.filter((j) => j.status === "scheduled").length ?? 0;
  const statusParam = (params.get("status") ?? "all") as StatusTab;
  const status: StatusTab = STATUS_TABS.some((t) => t.value === statusParam) ? statusParam : "all";
  const agentId = params.get("agent_id") ?? "";
  const conversationId = params.get("conversation_id") ?? "";
  const page = Math.max(0, Number(params.get("page") ?? 0) || 0);
  const [query, setQuery] = useState(params.get("q") ?? "");
  const q = useDebounced(query.trim(), 300);

  const setParam = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params.toString());
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === "" || (k === "status" && v === "all")) next.delete(k);
      else next.set(k, v);
    }
    if (!("page" in patch)) next.delete("page");
    const s = next.toString();
    router.replace(`${pathname}${s ? `?${s}` : ""}`, { scroll: false });
  };

  const tasks = useTasks({
    status: status === "all" ? "" : (status as TaskStatus),
    agent_id: agentId || undefined,
    conversation_id: conversationId || undefined,
    q: q || undefined,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  });

  const items = tasks.data?.items ?? [];
  const total = tasks.data?.total ?? 0;
  const from = total === 0 ? 0 : page * PAGE_SIZE + 1;
  const to = Math.min(total, page * PAGE_SIZE + items.length);
  const filtered = status !== "all" || !!agentId || !!conversationId || !!q;

  return (
    <PageContainer>
      <PageHeader
        title="Tasks"
        description="Everything the swarm is working on — live."
        actions={
          <>
            <ButtonLink href="/schedules" icon={<CalendarClock className="size-4" />}>
              Schedules
              {scheduledCount > 0 && (
                <span className="ml-0.5 inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-surface-3 px-1.5 text-[10.5px] font-semibold text-muted">
                  {scheduledCount}
                </span>
              )}
            </ButtonLink>
            {canWrite && (
              <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setNewOpen(true)}>
                New task
              </Button>
            )}
          </>
        }
      />

      <div className="mb-4 space-y-3">
        <Tabs<StatusTab>
          aria-label="Filter by status"
          value={status}
          onValueChange={(v) => setParam({ status: v })}
          items={STATUS_TABS.map((t) => ({ ...t, count: t.value === status && tasks.data ? total : undefined }))}
        />
        <div className="flex flex-wrap items-center gap-2">
          <Input
            leftIcon={<Search />}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              if (page) setParam({ page: null });
            }}
            placeholder="Search tasks"
            aria-label="Search tasks"
            wrapperClassName="w-full sm:w-72"
          />
          <Select
            value={agentId}
            onChange={(e) => setParam({ agent_id: e.target.value })}
            aria-label="Filter by agent"
            wrapperClassName="w-full sm:w-56"
          >
            <option value="">All agents</option>
            {(agents.data?.items ?? []).map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </Select>
          {conversationId && (
            <Badge tone="accent" size="md" className="pr-1">
              <span>This conversation</span>
              <button
                type="button"
                aria-label="Clear conversation filter"
                onClick={() => setParam({ conversation_id: null })}
                className="ml-1 inline-flex size-4 items-center justify-center rounded-full hover:bg-fg/10"
              >
                <X className="size-3" />
              </button>
            </Badge>
          )}
        </div>
      </div>

      <Card className="overflow-hidden">
        {tasks.isLoading ? (
          <SkeletonRows rows={8} />
        ) : tasks.error && !tasks.data ? (
          <ErrorState error={tasks.error} onRetry={() => tasks.refetch()} />
        ) : items.length === 0 ? (
          <EmptyState
            icon={<ListChecks />}
            title={filtered ? "No matching tasks" : "No tasks yet"}
            description={
              filtered
                ? "Try a different status, agent or search."
                : "When Ren delegates work — or you assign a task directly — it shows up here with live progress."
            }
            action={
              filtered ? (
                <Button
                  size="sm"
                  onClick={() => {
                    setQuery("");
                    router.replace(pathname);
                  }}
                >
                  Clear filters
                </Button>
              ) : canWrite ? (
                <Button size="sm" variant="primary" onClick={() => setNewOpen(true)}>
                  Assign a task
                </Button>
              ) : undefined
            }
          />
        ) : (
          <div className={tasks.isFetching && tasks.isPlaceholderData ? "opacity-60 transition-opacity" : ""}>
            <TaskTable tasks={items} />
          </div>
        )}
        {total > PAGE_SIZE && (
          <div className="flex items-center justify-between border-t border-border px-4 py-2.5 text-xs text-muted">
            <span className="tabular-nums">
              {from}–{to} of {total}
            </span>
            <div className="flex gap-1">
              <Button
                size="xs"
                variant="ghost"
                icon={<ChevronLeft className="size-3.5" />}
                disabled={page === 0}
                onClick={() => setParam({ page: String(page - 1) })}
              >
                Previous
              </Button>
              <Button
                size="xs"
                variant="ghost"
                iconRight={<ChevronRight className="size-3.5" />}
                disabled={to >= total}
                onClick={() => setParam({ page: String(page + 1) })}
              >
                Next
              </Button>
            </div>
          </div>
        )}
      </Card>
      <NewTaskDialog open={newOpen} onOpenChange={setNewOpen} />
    </PageContainer>
  );
}
