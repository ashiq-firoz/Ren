import type { QueryClient } from "@tanstack/react-query";
import {
  appendMessageDelta,
  appendTaskEvent,
  prependNotification,
  setConversationStatus,
  upsertApproval,
  upsertConversation,
  upsertDocument,
  upsertMessage,
  upsertTask,
} from "./cache";
import { qk } from "./query-keys";
import { toast } from "./toast";
import type { StreamEvent, Task, TaskDetail, TaskStatus } from "./types";

const isTaskTerminal = (s: TaskStatus) => s === "completed" || s === "failed" || s === "cancelled";

function throttle(fn: () => void, ms: number) {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let last = 0;
  const run = () => {
    timer = null;
    last = Date.now();
    fn();
  };
  const call = () => {
    if (timer) return;
    const wait = Math.max(0, ms - (Date.now() - last));
    timer = setTimeout(run, wait);
  };
  call.cancel = () => {
    if (timer) clearTimeout(timer);
    timer = null;
  };
  return call;
}

/**
 * Turns stream events into TanStack Query cache writes. Text deltas are batched
 * per animation frame (or ~50ms in background tabs) so long replies don't
 * re-render on every token.
 */
export function createStreamDispatcher(qc: QueryClient) {
  const pending = new Map<string, { conversationId: string; text: string }>();
  let frame: number | null = null;
  let timeout: ReturnType<typeof setTimeout> | null = null;

  const flush = () => {
    if (frame !== null) cancelAnimationFrame(frame);
    if (timeout !== null) clearTimeout(timeout);
    frame = null;
    timeout = null;
    if (pending.size === 0) return;
    const batch = Array.from(pending.entries());
    pending.clear();
    for (const [messageId, { conversationId, text }] of batch) {
      appendMessageDelta(qc, conversationId, messageId, text);
    }
  };

  const scheduleFlush = () => {
    if (frame !== null || timeout !== null) return;
    if (typeof document !== "undefined" && document.visibilityState === "visible") {
      frame = requestAnimationFrame(flush);
    } else {
      timeout = setTimeout(flush, 50);
    }
  };

  const invalidateDashboard = throttle(() => void qc.invalidateQueries({ queryKey: qk.dashboard }), 4000);
  const invalidateAgents = throttle(() => void qc.invalidateQueries({ queryKey: qk.agents }), 5000);
  const invalidateKnowledge = throttle(() => {
    void qc.invalidateQueries({ queryKey: qk.collections });
    void qc.invalidateQueries({ queryKey: qk.knowledgeStats });
  }, 3000);
  const invalidateAlignment = throttle(() => {
    void qc.invalidateQueries({ queryKey: ["alignment"] });
  }, 6000);
  // A scheduled run changes the job's next run / run count; outputs list new files as agents write them.
  const invalidateSchedules = throttle(() => void qc.invalidateQueries({ queryKey: qk.schedules }), 2000);
  const invalidateArtifacts = throttle(() => void qc.invalidateQueries({ queryKey: qk.artifactLists }), 1500);

  function previousStatus(task: Task) {
    return qc.getQueryData<TaskDetail>(qk.task(task.id))?.status;
  }

  function handle(event: StreamEvent) {
    if (event.type === "ping" || event.type === "ready") return;
    if (event.type === "message.delta") {
      const { conversation_id, message_id, delta } = event.data;
      if (!delta) return;
      const entry = pending.get(message_id);
      if (entry) entry.text += delta;
      else pending.set(message_id, { conversationId: conversation_id, text: delta });
      scheduleFlush();
      return;
    }

    // Preserve ordering: apply buffered text before any other event.
    flush();

    switch (event.type) {
      case "message.created":
      case "message.completed":
      case "message.updated": {
        upsertMessage(qc, event.data.message);
        if (event.type !== "message.created" && event.data.message.metadata?.alignment) invalidateAlignment();
        break;
      }
      case "conversation.created":
      case "conversation.updated":
        upsertConversation(qc, event.data.conversation);
        break;
      case "conversation.status":
        setConversationStatus(qc, event.data);
        break;
      case "task.created":
      case "task.updated": {
        const task = event.data.task;
        const before = previousStatus(task);
        upsertTask(qc, task);
        if (event.type === "task.created" || before !== task.status) {
          invalidateDashboard();
          invalidateAgents();
        }
        if (task.schedule_id && (event.type === "task.created" || isTaskTerminal(task.status))) invalidateSchedules();
        break;
      }
      case "task.event":
        appendTaskEvent(qc, event.data.task_id, event.data.event);
        if (event.data.event.kind === "artifact") invalidateArtifacts();
        break;
      case "approval.requested":
      case "approval.resolved":
        upsertApproval(qc, event.data.approval);
        invalidateDashboard();
        break;
      case "document.updated":
        upsertDocument(qc, event.data.document);
        if (event.data.document.status === "ready" || event.data.document.status === "failed") invalidateKnowledge();
        break;
      case "notification.created": {
        const n = event.data.notification;
        prependNotification(qc, n);
        const variant =
          /fail|error|block/i.test(n.kind) ? "error" : /approval/i.test(n.kind) ? "warning" : /complete|success/i.test(n.kind) ? "success" : "info";
        toast.show(variant, n.title, {
          description: n.body || undefined,
          action: n.link ? { label: "Open", href: n.link } : undefined,
        });
        break;
      }
      default:
        break;
    }
  }

  function dispose() {
    flush();
    invalidateDashboard.cancel();
    invalidateAgents.cancel();
    invalidateKnowledge.cancel();
    invalidateAlignment.cancel();
    invalidateSchedules.cancel();
    invalidateArtifacts.cancel();
  }

  return { handle, dispose, flush };
}
