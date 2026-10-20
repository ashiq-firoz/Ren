"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { AgentAvatar } from "@/components/ui/avatar";
import { ProgressBar } from "@/components/ui/feedback";
import { Table, TableContainer, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useAgentsMap } from "@/hooks/queries";
import { useNow } from "@/hooks/use-browser";
import { formatDuration, formatNumber, timeAgo } from "@/lib/format";
import type { Task } from "@/lib/types";
import { cn } from "@/lib/utils";
import { isTaskActive, PriorityBadge, TaskStatusPill } from "./status";

function elapsedOf(t: Task, now: number): number | null {
  if (!t.started_at) return null;
  const start = Date.parse(t.started_at);
  const end = t.finished_at ? Date.parse(t.finished_at) : now || start;
  return Math.max(0, (end - start) / 1000);
}

export function TaskTable({ tasks, compact, className }: { tasks: Task[]; compact?: boolean; className?: string }) {
  const router = useRouter();
  const agents = useAgentsMap();
  const anyRunning = tasks.some((t) => t.status === "running");
  const now = useNow(anyRunning);
  return (
    <TableContainer className={className}>
      <Table>
        <THead>
          <tr>
            <TH>Task</TH>
            {!compact && <TH>Agent</TH>}
            <TH>Status</TH>
            <TH className="w-[140px]">Progress</TH>
            <TH>Started</TH>
            <TH className="text-right">Duration</TH>
            {!compact && <TH className="text-right">Tokens</TH>}
          </tr>
        </THead>
        <TBody>
          {tasks.map((t) => {
            const agent = agents.get(t.agent_id);
            const elapsed = elapsedOf(t, now);
            return (
              <TR key={t.id} interactive onClick={() => router.push(`/tasks/${t.id}`)}>
                <TD className="max-w-[360px]">
                  <div className="flex items-center gap-2.5">
                    {compact && <AgentAvatar agent={agent} name={t.agent_name} color={agent?.color ?? t.agent_color} size="sm" />}
                    <div className="min-w-0">
                      <Link
                        href={`/tasks/${t.id}`}
                        onClick={(e) => e.stopPropagation()}
                        className="block truncate font-medium text-fg hover:underline"
                      >
                        {t.title}
                      </Link>
                      {compact ? (
                        <span className="block truncate text-xs text-muted">{t.agent_name}</span>
                      ) : (
                        t.current_step &&
                        isTaskActive(t.status) && <span className="block truncate text-xs text-muted">{t.current_step}</span>
                      )}
                    </div>
                    <PriorityBadge priority={t.priority} />
                  </div>
                </TD>
                {!compact && (
                  <TD>
                    <span className="flex items-center gap-2 whitespace-nowrap">
                      <AgentAvatar agent={agent} name={t.agent_name} color={agent?.color ?? t.agent_color} size="xs" />
                      <span className="truncate text-muted">{t.agent_name}</span>
                    </span>
                  </TD>
                )}
                <TD>
                  <TaskStatusPill status={t.status} />
                </TD>
                <TD>
                  <div className="flex items-center gap-2">
                    <ProgressBar
                      value={t.status === "completed" ? 100 : t.progress}
                      size="xs"
                      tone={t.status === "completed" ? "success" : t.status === "failed" ? "danger" : "accent"}
                      className="w-20"
                    />
                    <span className="w-8 text-right text-xs text-subtle tabular-nums">
                      {Math.round(t.status === "completed" ? 100 : t.progress)}%
                    </span>
                  </div>
                </TD>
                <TD className="whitespace-nowrap text-muted">{t.started_at ? timeAgo(t.started_at) : "—"}</TD>
                <TD className={cn("text-right whitespace-nowrap text-muted tabular-nums")}>{formatDuration(elapsed)}</TD>
                {!compact && <TD className="text-right text-muted tabular-nums">{formatNumber(t.tokens, { compact: true })}</TD>}
              </TR>
            );
          })}
        </TBody>
      </Table>
    </TableContainer>
  );
}
