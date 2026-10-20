import type { QueryClient, QueryKey } from "@tanstack/react-query";
import {
  qk,
  type ApprovalFilter,
  type ConversationListParams,
  type DocumentListParams,
  type ScheduleFilter,
  type TaskListParams,
} from "./query-keys";
import { scheduleMatchesFilter } from "./schedules";
import type {
  Agent,
  Approval,
  Conversation,
  ConversationDetail,
  ConversationStatus,
  ConversationStatusEvent,
  DashboardResponse,
  KDocument,
  KDocumentDetail,
  ListResponse,
  Message,
  Notification,
  ScheduledJob,
  ScheduleDetail,
  ScheduleRun,
  Task,
  TaskDetail,
  TaskEvent,
} from "./types";

/* ------------------------------------------------------------------ */
/* Generic list helpers                                                */
/* ------------------------------------------------------------------ */

type WithId = { id: string };

export function upsertById<T extends WithId>(items: T[], item: T, opts: { prepend?: boolean } = {}): T[] {
  const idx = items.findIndex((x) => x.id === item.id);
  if (idx >= 0) {
    const next = items.slice();
    next[idx] = { ...items[idx], ...item };
    return next;
  }
  return opts.prepend ? [item, ...items] : [...items, item];
}

export function upsertInList<T extends WithId>(
  list: ListResponse<T>,
  item: T,
  opts: { prepend?: boolean } = {},
): ListResponse<T> {
  const exists = list.items.some((x) => x.id === item.id);
  return {
    items: upsertById(list.items, item, opts),
    total: exists ? list.total : list.total + 1,
  };
}

export function removeFromList<T extends WithId>(list: ListResponse<T>, id: string): ListResponse<T> {
  const items = list.items.filter((x) => x.id !== id);
  return { items, total: Math.max(0, list.total - (list.items.length - items.length)) };
}

function eachQuery<T>(qc: QueryClient, prefix: QueryKey, fn: (key: QueryKey, data: T) => T | undefined) {
  for (const [key, data] of qc.getQueriesData<T>({ queryKey: prefix })) {
    if (data === undefined) continue;
    const next = fn(key, data);
    if (next !== undefined && next !== data) qc.setQueryData(key, next);
  }
}

const byCreatedDesc = (a: { created_at: string }, b: { created_at: string }) =>
  b.created_at.localeCompare(a.created_at);

/* ------------------------------------------------------------------ */
/* Messages                                                            */
/* ------------------------------------------------------------------ */

export const TEMP_PREFIX = "tmp-";
export const isTempId = (id: string) => id.startsWith(TEMP_PREFIX);

/** Temp ids already reconciled with a server message (so a late 202 doesn't duplicate). */
const reconciled = new Map<string, string>();

export function getReconciledId(tempId: string) {
  return reconciled.get(tempId);
}

function mergeMessage(existing: Message, incoming: Message): Message {
  // A `message.created` for a message we already accumulated deltas for must not wipe text.
  if (incoming.status === "streaming" && existing.content.length > incoming.content.length) {
    return { ...existing, ...incoming, content: existing.content };
  }
  return { ...existing, ...incoming, metadata: incoming.metadata ?? existing.metadata };
}

function previewOf(message: Message): string | null {
  if (message.role !== "user" && message.role !== "assistant") return null;
  const text = message.content.replace(/\s+/g, " ").trim();
  return text ? text.slice(0, 160) : null;
}

/** Insert or replace a message in its conversation (reconciling optimistic user messages). */
export function upsertMessage(qc: QueryClient, message: Message) {
  qc.setQueryData<ConversationDetail>(qk.conversation(message.conversation_id), (old) => {
    if (!old) return old;
    const messages = old.messages;
    const idx = messages.findIndex((m) => m.id === message.id);
    if (idx >= 0) {
      const next = messages.slice();
      next[idx] = mergeMessage(messages[idx], message);
      return { ...old, messages: next };
    }
    if (message.role === "user") {
      const tmpIdx = messages.findIndex(
        (m) => isTempId(m.id) && m.role === "user" && m.content.trim() === message.content.trim(),
      );
      if (tmpIdx >= 0) {
        const next = messages.slice();
        reconciled.set(messages[tmpIdx].id, message.id);
        next[tmpIdx] = {
          ...message,
          metadata: { ...messages[tmpIdx].metadata, ...message.metadata },
        };
        return { ...old, messages: next };
      }
    }
    return { ...old, messages: [...messages, message] };
  });

  const preview = previewOf(message);
  if (preview && message.status !== "streaming") {
    patchConversation(qc, message.conversation_id, {
      last_message_preview: preview,
      updated_at: message.created_at,
    });
  }
}

/** Replace an optimistic message with the server copy returned by the 202 response. */
export function reconcileOptimistic(qc: QueryClient, conversationId: string, tempId: string, message: Message) {
  qc.setQueryData<ConversationDetail>(qk.conversation(conversationId), (old) => {
    if (!old) return old;
    const realIdx = old.messages.findIndex((m) => m.id === message.id);
    const tmpIdx = old.messages.findIndex((m) => m.id === tempId);
    if (realIdx >= 0) {
      // Stream got there first — just drop the temp copy.
      if (tmpIdx < 0) return old;
      return { ...old, messages: old.messages.filter((m) => m.id !== tempId) };
    }
    if (tmpIdx >= 0) {
      const next = old.messages.slice();
      next[tmpIdx] = { ...message, metadata: { ...old.messages[tmpIdx].metadata, ...message.metadata } };
      return { ...old, messages: next };
    }
    return { ...old, messages: [...old.messages, message] };
  });
  reconciled.set(tempId, message.id);
}

export function patchMessage(qc: QueryClient, conversationId: string, messageId: string, patch: Partial<Message>) {
  qc.setQueryData<ConversationDetail>(qk.conversation(conversationId), (old) => {
    if (!old) return old;
    const idx = old.messages.findIndex((m) => m.id === messageId);
    if (idx < 0) return old;
    const next = old.messages.slice();
    next[idx] = { ...next[idx], ...patch };
    return { ...old, messages: next };
  });
}

export function removeMessage(qc: QueryClient, conversationId: string, messageId: string) {
  qc.setQueryData<ConversationDetail>(qk.conversation(conversationId), (old) =>
    old ? { ...old, messages: old.messages.filter((m) => m.id !== messageId) } : old,
  );
}

/** Apply a batch of streamed text deltas for one message. */
export function appendMessageDelta(qc: QueryClient, conversationId: string, messageId: string, delta: string) {
  qc.setQueryData<ConversationDetail>(qk.conversation(conversationId), (old) => {
    if (!old) return old;
    const idx = old.messages.findIndex((m) => m.id === messageId);
    if (idx < 0) {
      // Missed `message.created` — create a placeholder so text still appears.
      const placeholder: Message = {
        id: messageId,
        conversation_id: conversationId,
        role: "assistant",
        content: delta,
        agent_id: null,
        task_id: null,
        status: "streaming",
        metadata: {},
        created_at: new Date().toISOString(),
        user_id: null,
      };
      return { ...old, messages: [...old.messages, placeholder] };
    }
    const msg = old.messages[idx];
    if (msg.status === "complete" || msg.status === "error") return old;
    const next = old.messages.slice();
    next[idx] = { ...msg, content: msg.content + delta, status: "streaming" };
    return { ...old, messages: next };
  });
}

/* ------------------------------------------------------------------ */
/* Conversations                                                       */
/* ------------------------------------------------------------------ */

export function sortConversations(items: Conversation[]): Conversation[] {
  return items.slice().sort((a, b) => {
    if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
    return b.updated_at.localeCompare(a.updated_at);
  });
}

function conversationMatches(conv: Conversation, params: Required<ConversationListParams>) {
  if (conv.archived !== params.archived) return false;
  if (params.q && !conv.title.toLowerCase().includes(params.q.toLowerCase())) return false;
  return true;
}

export function upsertConversation(qc: QueryClient, conv: Conversation) {
  eachQuery<ListResponse<Conversation>>(qc, qk.conversationLists, (key, data) => {
    const params = (key[1] ?? { archived: false, q: "" }) as Required<ConversationListParams>;
    const matches = conversationMatches(conv, params);
    const exists = data.items.some((c) => c.id === conv.id);
    if (exists && !matches) return removeFromList(data, conv.id);
    if (!exists && !matches) return undefined;
    const next = upsertInList(data, conv, { prepend: true });
    return { ...next, items: sortConversations(next.items) };
  });
  qc.setQueryData<ConversationDetail>(qk.conversation(conv.id), (old) =>
    old ? { ...old, ...conv, messages: old.messages } : old,
  );
  qc.setQueryData<LiveConversationStatus>(qk.conversationStatus(conv.id), (old) =>
    old && old.status !== conv.status ? { ...old, status: conv.status, queued: conv.status === "idle" ? 0 : old.queued } : old,
  );
}

export function patchConversation(qc: QueryClient, id: string, patch: Partial<Conversation>) {
  eachQuery<ListResponse<Conversation>>(qc, qk.conversationLists, (_key, data) => {
    const idx = data.items.findIndex((c) => c.id === id);
    if (idx < 0) return undefined;
    const items = data.items.slice();
    items[idx] = { ...items[idx], ...patch };
    return { ...data, items: patch.updated_at || patch.pinned !== undefined ? sortConversations(items) : items };
  });
  qc.setQueryData<ConversationDetail>(qk.conversation(id), (old) => (old ? { ...old, ...patch } : old));
}

export function removeConversation(qc: QueryClient, id: string) {
  eachQuery<ListResponse<Conversation>>(qc, qk.conversationLists, (_key, data) =>
    data.items.some((c) => c.id === id) ? removeFromList(data, id) : undefined,
  );
  qc.removeQueries({ queryKey: qk.conversation(id) });
}

export interface LiveConversationStatus {
  status: ConversationStatus;
  detail: string | null;
  queued: number;
}

export function setConversationStatus(qc: QueryClient, ev: ConversationStatusEvent) {
  qc.setQueryData<LiveConversationStatus>(qk.conversationStatus(ev.conversation_id), {
    status: ev.status,
    detail: ev.detail,
    queued: ev.queued ?? 0,
  });
  patchConversation(qc, ev.conversation_id, { status: ev.status });
}

/* ------------------------------------------------------------------ */
/* Tasks                                                               */
/* ------------------------------------------------------------------ */

export function taskMatches(task: Task, params: TaskListParams) {
  if (params.status && task.status !== params.status) return false;
  if (params.agent_id && task.agent_id !== params.agent_id) return false;
  if (params.conversation_id && task.conversation_id !== params.conversation_id) return false;
  if (params.q && !task.title.toLowerCase().includes(params.q.toLowerCase())) return false;
  return true;
}

export function upsertTask(qc: QueryClient, task: Task) {
  eachQuery<ListResponse<Task>>(qc, qk.taskLists, (key, data) => {
    const params = (key[2] ?? {}) as TaskListParams;
    const matches = taskMatches(task, params);
    const exists = data.items.some((t) => t.id === task.id);
    if (exists) return matches ? upsertInList(data, task) : removeFromList(data, task.id);
    if (!matches || (params.offset ?? 0) > 0) return undefined;
    const next = upsertInList(data, task, { prepend: true });
    return { ...next, items: next.items.slice().sort(byCreatedDesc) };
  });

  qc.setQueryData<TaskDetail>(qk.task(task.id), (old) =>
    old ? { ...old, ...task, events: old.events, children: old.children, approvals: old.approvals } : old,
  );

  if (task.parent_id) {
    qc.setQueryData<TaskDetail>(qk.task(task.parent_id), (old) =>
      old ? { ...old, children: upsertById(old.children ?? [], task) } : old,
    );
  }

  if (task.conversation_id) {
    qc.setQueryData<ListResponse<Task>>(qk.conversationTasks(task.conversation_id), (old) =>
      old ? upsertInList(old, task, { prepend: true }) : old,
    );
  }

  qc.setQueryData<DashboardResponse>(qk.dashboard, (old) =>
    old && old.recent_tasks.some((t) => t.id === task.id)
      ? { ...old, recent_tasks: old.recent_tasks.map((t) => (t.id === task.id ? task : t)) }
      : old,
  );

  if (task.schedule_id) upsertScheduleRun(qc, task);
}

export function appendTaskEvent(qc: QueryClient, taskId: string, event: TaskEvent) {
  qc.setQueryData<TaskDetail>(qk.task(taskId), (old) => {
    if (!old) return old;
    if (old.events.some((e) => e.id === event.id)) return old;
    return { ...old, events: [...old.events, event] };
  });
}

/* ------------------------------------------------------------------ */
/* Approvals                                                           */
/* ------------------------------------------------------------------ */

export function upsertApproval(qc: QueryClient, approval: Approval) {
  eachQuery<ListResponse<Approval>>(qc, qk.approvalLists, (key, data) => {
    const filter = key[1] as ApprovalFilter;
    const matches = filter === "all" || filter === approval.status;
    const exists = data.items.some((a) => a.id === approval.id);
    if (exists) return matches ? upsertInList(data, approval) : removeFromList(data, approval.id);
    if (!matches) return undefined;
    return upsertInList(data, approval, { prepend: true });
  });
  if (approval.task_id) {
    qc.setQueryData<TaskDetail>(qk.task(approval.task_id), (old) =>
      old ? { ...old, approvals: upsertById(old.approvals ?? [], approval) } : old,
    );
  }
}

/* ------------------------------------------------------------------ */
/* Documents                                                           */
/* ------------------------------------------------------------------ */

function documentMatches(doc: KDocument, params: DocumentListParams) {
  if (params.collection_id && params.collection_id !== doc.collection_id) return false;
  if (params.status && params.status !== doc.status) return false;
  if (params.q && !`${doc.title} ${doc.filename}`.toLowerCase().includes(params.q.toLowerCase())) return false;
  return true;
}

export function upsertDocument(qc: QueryClient, doc: KDocument) {
  eachQuery<ListResponse<KDocument>>(qc, qk.documentLists, (key, data) => {
    const params = (key[2] ?? {}) as DocumentListParams;
    const matches = documentMatches(doc, params);
    const exists = data.items.some((d) => d.id === doc.id);
    if (exists) return matches ? upsertInList(data, doc) : removeFromList(data, doc.id);
    if (!matches) return undefined;
    return upsertInList(data, doc, { prepend: true });
  });
  let becameReady = false;
  qc.setQueryData<KDocumentDetail>(qk.document(doc.id), (old) => {
    if (!old) return old;
    becameReady = old.status !== "ready" && doc.status === "ready";
    return { ...old, ...doc, toc: old.toc };
  });
  if (becameReady) {
    void qc.invalidateQueries({ queryKey: qk.document(doc.id) });
    void qc.invalidateQueries({ queryKey: qk.knowledgeStats });
  }
}

export function removeDocument(qc: QueryClient, id: string) {
  eachQuery<ListResponse<KDocument>>(qc, qk.documentLists, (_key, data) =>
    data.items.some((d) => d.id === id) ? removeFromList(data, id) : undefined,
  );
  qc.removeQueries({ queryKey: qk.document(id) });
}

/* ------------------------------------------------------------------ */
/* Schedules                                                           */
/* ------------------------------------------------------------------ */

const byRunAt = (a: ScheduledJob, b: ScheduledJob) => a.run_at.localeCompare(b.run_at);

/** Write a job returned by a mutation into every schedule list (moving it between Active/History) and its detail. */
export function upsertSchedule(qc: QueryClient, job: ScheduledJob) {
  eachQuery<ListResponse<ScheduledJob>>(qc, qk.scheduleLists, (key, data) => {
    const filter = (key[2] ?? "active") as ScheduleFilter;
    const matches = scheduleMatchesFilter(job, filter);
    const exists = data.items.some((j) => j.id === job.id);
    if (exists) return matches ? upsertInList(data, job) : removeFromList(data, job.id);
    if (!matches) return undefined;
    // History is newest first; active lists are ordered by next run.
    if (filter === "history") return upsertInList(data, job, { prepend: true });
    const next = upsertInList(data, job);
    return { ...next, items: next.items.slice().sort(byRunAt) };
  });
  qc.setQueryData<ScheduleDetail>(qk.schedule(job.id), (old) => (old ? { ...old, job: { ...old.job, ...job } } : old));
}

/** Keep a schedule's run history and "last run" pill live from task stream updates. */
export function upsertScheduleRun(qc: QueryClient, task: Task) {
  const scheduleId = task.schedule_id;
  if (!scheduleId) return;
  const lastTask = { id: task.id, title: task.title, status: task.status, created_at: task.created_at, finished_at: task.finished_at };
  // Only the newest run is the job's "last run".
  const patchJob = (job: ScheduledJob): ScheduledJob =>
    !job.last_task || job.last_task.id === task.id || job.last_task.created_at <= task.created_at ? { ...job, last_task: lastTask } : job;

  qc.setQueryData<ScheduleDetail>(qk.schedule(scheduleId), (old) => {
    if (!old) return old;
    const runs = old.runs ?? [];
    const prev = runs.find((r) => r.id === task.id);
    const run: ScheduleRun = {
      id: task.id,
      title: task.title,
      status: task.status,
      created_at: task.created_at,
      started_at: task.started_at,
      finished_at: task.finished_at,
      error: task.error,
      // Partial task payloads shouldn't wipe the files we already know about.
      artifacts: Array.isArray(task.artifacts) ? task.artifacts : (prev?.artifacts ?? []),
    };
    const next = prev ? runs.map((r) => (r.id === task.id ? run : r)) : [run, ...runs];
    return { ...old, job: patchJob(old.job), runs: next.sort(byCreatedDesc) };
  });
  eachQuery<ListResponse<ScheduledJob>>(qc, qk.scheduleLists, (_key, data) => {
    const idx = data.items.findIndex((j) => j.id === scheduleId);
    if (idx < 0) return undefined;
    const items = data.items.slice();
    items[idx] = patchJob(items[idx]);
    return { ...data, items };
  });
}

/* ------------------------------------------------------------------ */
/* Notifications & agents                                              */
/* ------------------------------------------------------------------ */

export function prependNotification(qc: QueryClient, n: Notification) {
  qc.setQueryData<ListResponse<Notification>>(qk.notifications, (old) =>
    old ? upsertInList(old, n, { prepend: true }) : old,
  );
}

export function upsertAgent(qc: QueryClient, agent: Agent) {
  qc.setQueryData<ListResponse<Agent>>(qk.agents, (old) => (old ? upsertInList(old, agent) : old));
  qc.setQueryData<Agent>(qk.agent(agent.id), (old) => (old ? { ...old, ...agent } : agent));
}
