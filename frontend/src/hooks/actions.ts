"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { upsertApproval, upsertSchedule, upsertTask } from "@/lib/cache";
import { formatDateTime } from "@/lib/format";
import { qk } from "@/lib/query-keys";
import { scheduleSummary } from "@/lib/schedules";
import { toast } from "@/lib/toast";
import type { Approval, ScheduledJob, ScheduleRunNowResponse, Task, TaskEvent, TaskDetail } from "@/lib/types";

export type TaskAction = "cancel" | "pause" | "resume" | "retry";

const ACTION_DONE: Record<TaskAction, string> = {
  cancel: "Task cancelled",
  pause: "Task paused",
  resume: "Task resumed",
  retry: "Task restarted",
};

/** Cancel / pause / resume / retry a task; the response updates every cached view. */
export function useTaskAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, action }: { id: string; action: TaskAction }) => api.post<Task>(`/tasks/${id}/${action}`),
    onSuccess: (task, { action }) => {
      upsertTask(qc, task);
      toast.success(ACTION_DONE[action], { description: task.title });
    },
    meta: { errorTitle: "Task action failed" },
  });
}

/** Send a message into a running agent's inbox. */
export function useTaskMessage(taskId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (content: string) => api.post<TaskEvent>(`/tasks/${taskId}/message`, { content }),
    onSuccess: (event) => {
      qc.setQueryData<TaskDetail>(qk.task(taskId), (old) =>
        old && event && !old.events.some((e) => e.id === event.id) ? { ...old, events: [...old.events, event] } : old,
      );
    },
    meta: { errorTitle: "Couldn't message the agent" },
  });
}

export interface ApprovalDecision {
  id: string;
  decision: "approve" | "reject";
  comment?: string;
  modified_args?: Record<string, unknown>;
}

export function useApprovalDecision() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, decision, comment, modified_args }: ApprovalDecision) =>
      api.post<Approval>(
        `/approvals/${id}/${decision}`,
        decision === "approve" ? { comment: comment || undefined, modified_args } : { comment: comment || undefined },
      ),
    onSuccess: (approval) => {
      upsertApproval(qc, approval);
      void qc.invalidateQueries({ queryKey: qk.dashboard });
      toast.success(approval.status === "approved" ? "Approved" : "Rejected", {
        description: approval.tool_display,
      });
    },
    meta: { errorTitle: "Couldn't record your decision" },
  });
}

/* ---------------- Schedules ---------------- */

export interface ScheduleInput {
  title: string;
  agent_id: string;
  instructions: string;
  /** Recurring: 5-field cron read in `timezone`. */
  cron?: string | null;
  /** One-off: local wall-clock "YYYY-MM-DDTHH:MM" read in `timezone`. */
  run_at?: string | null;
  timezone?: string | null;
}

export type SchedulePatch = Partial<ScheduleInput>;

function useInvalidateSchedules() {
  const qc = useQueryClient();
  return (job?: ScheduledJob) => {
    if (job) upsertSchedule(qc, job);
    // Reconcile with the server's ordering, next run and run counts.
    void qc.invalidateQueries({ queryKey: qk.schedules });
  };
}

export function useCreateSchedule() {
  const refresh = useInvalidateSchedules();
  return useMutation({
    mutationFn: (body: ScheduleInput) => api.post<ScheduledJob>("/schedules", body),
    onSuccess: (job) => {
      refresh(job);
      toast.success("Schedule created", { description: `${job.title} · ${scheduleSummary(job)}` });
    },
    meta: { errorTitle: "Couldn't create the schedule" },
  });
}

export function useUpdateSchedule() {
  const refresh = useInvalidateSchedules();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: SchedulePatch }) => api.patch<ScheduledJob>(`/schedules/${id}`, patch),
    onSuccess: (job) => {
      refresh(job);
      toast.success("Schedule updated", { description: `${job.title} · ${scheduleSummary(job)}` });
    },
    meta: { errorTitle: "Couldn't save the schedule" },
  });
}

/** Pause or resume (recurring jobs resume at their next occurrence). */
export function useScheduleToggle() {
  const refresh = useInvalidateSchedules();
  return useMutation({
    mutationFn: ({ job, action }: { job: ScheduledJob; action: "pause" | "resume" }) =>
      api.post<ScheduledJob>(`/schedules/${job.id}/${action}`),
    onSuccess: (job, { action }) => {
      refresh(job);
      if (action === "pause") toast.success("Schedule paused", { description: job.title });
      else toast.success("Schedule resumed", { description: `Next run ${formatDateTime(job.run_at, { weekday: true })}` });
    },
    meta: { errorTitle: "Couldn't update the schedule" },
  });
}

/** Run once now — doesn't change the schedule, but counts as a run. */
export function useRunScheduleNow() {
  const qc = useQueryClient();
  const refresh = useInvalidateSchedules();
  return useMutation({
    mutationFn: (job: ScheduledJob) => api.post<ScheduleRunNowResponse>(`/schedules/${job.id}/run`),
    onSuccess: (res, job) => {
      const task = res?.task ?? null;
      if (task) upsertTask(qc, task);
      refresh();
      if (task) {
        toast.success("Run started", {
          description: `${task.agent_name || job.agent_name || "The agent"} is working on “${task.title}”.`,
          action: { label: "View task", href: `/tasks/${task.id}` },
        });
      } else {
        toast.success(job.kind === "notification" ? "Reminder sent" : "Run started", { description: job.title });
      }
    },
    meta: { errorTitle: "Couldn't start the run" },
  });
}

/** Cancels the job server-side; it moves to History. */
export function useDeleteSchedule() {
  const refresh = useInvalidateSchedules();
  return useMutation({
    mutationFn: (job: ScheduledJob) => api.delete(`/schedules/${job.id}`),
    onSuccess: (_res, job) => {
      refresh({ ...job, status: "cancelled" });
      toast.success("Schedule deleted", { description: job.title });
    },
    meta: { errorTitle: "Couldn't delete the schedule" },
  });
}
