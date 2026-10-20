"use client";

import { Bot } from "lucide-react";
import Link from "next/link";
import { AgentAvatar, Avatar } from "@/components/ui/avatar";
import type { Agent, AuditEntry } from "@/lib/types";
import { titleCase } from "@/lib/utils";

/** "task.created" → "Task created", "approval.approve" → "Approval approve". */
export function humanizeAction(action: string): string {
  const words = action.replace(/[._:/-]+/g, " ").trim();
  if (!words) return action;
  return words.charAt(0).toUpperCase() + words.slice(1).toLowerCase();
}

export function actorName(e: AuditEntry): string {
  return e.agent_name ?? e.user_name ?? "System";
}

export function ActorAvatar({ entry, agent, size = "sm" }: { entry: AuditEntry; agent?: Agent; size?: "xs" | "sm" | "md" }) {
  if (entry.agent_id || entry.agent_name) return <AgentAvatar agent={agent} name={entry.agent_name} size={size} />;
  if (entry.user_name) return <Avatar name={entry.user_name} size={size} />;
  return (
    <span className="inline-flex size-6 items-center justify-center rounded-md bg-surface-2 text-muted">
      <Bot className="size-3.5" />
    </span>
  );
}

export function targetHref(e: AuditEntry): string | null {
  if (e.task_id) return `/tasks/${e.task_id}`;
  if (!e.target_id || !e.target_type) return null;
  switch (e.target_type) {
    case "task":
      return `/tasks/${e.target_id}`;
    case "agent":
      return `/agents/${e.target_id}`;
    case "conversation":
      return `/chat/${e.target_id}`;
    case "document":
      return `/knowledge/${e.target_id}`;
    case "approval":
      return "/approvals";
    case "policy":
      return "/policies";
    default:
      return null;
  }
}

export function AuditTarget({ entry }: { entry: AuditEntry }) {
  if (!entry.target_type) return <span className="text-subtle">—</span>;
  const href = targetHref(entry);
  const label = `${titleCase(entry.target_type)}${entry.details?.title ? ` · ${String(entry.details.title)}` : entry.details?.name ? ` · ${String(entry.details.name)}` : ""}`;
  return href ? (
    <Link href={href} onClick={(e) => e.stopPropagation()} className="truncate text-fg hover:underline">
      {label}
    </Link>
  ) : (
    <span className="truncate text-muted">{label}</span>
  );
}
