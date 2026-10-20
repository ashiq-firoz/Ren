"use client";

import { Loader2, ShieldAlert, ShieldCheck, ShieldX } from "lucide-react";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { ProgressBar } from "@/components/ui/feedback";
import type {
  AlignmentVerdict,
  Autonomy,
  DocumentStatus,
  KDocument,
  Priority,
  Risk,
  Role,
  StructureSource,
  TaskStatus,
} from "@/lib/types";
import { cn } from "@/lib/utils";

/* ---------------- Tasks ---------------- */

export const TASK_STATUS_LABEL: Record<TaskStatus, string> = {
  queued: "Queued",
  running: "Running",
  waiting_approval: "Waiting approval",
  paused: "Paused",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

export const TASK_STATUS_TONE: Record<TaskStatus, BadgeTone> = {
  queued: "neutral",
  running: "accent",
  waiting_approval: "warning",
  paused: "info",
  completed: "success",
  failed: "danger",
  cancelled: "outline",
};

export const ACTIVE_TASK_STATUSES: TaskStatus[] = ["queued", "running", "waiting_approval", "paused"];
export const isTaskActive = (s: TaskStatus) => ACTIVE_TASK_STATUSES.includes(s);
export const isTaskTerminal = (s: TaskStatus) => s === "completed" || s === "failed" || s === "cancelled";

export function TaskStatusPill({ status, size = "sm", className }: { status: TaskStatus; size?: "xs" | "sm" | "md"; className?: string }) {
  return (
    <Badge
      tone={TASK_STATUS_TONE[status] ?? "neutral"}
      size={size}
      dot
      pulse={status === "running"}
      className={className}
    >
      {TASK_STATUS_LABEL[status] ?? status}
    </Badge>
  );
}

const PRIORITY_TONE: Record<Priority, BadgeTone> = { low: "outline", normal: "neutral", high: "warning", urgent: "danger" };

export function PriorityBadge({ priority }: { priority: Priority }) {
  if (priority === "normal") return null;
  return (
    <Badge tone={PRIORITY_TONE[priority]} size="xs">
      {priority}
    </Badge>
  );
}

/* ---------------- Risk ---------------- */

export const RISK_TONE: Record<Risk, BadgeTone> = {
  low: "success",
  medium: "accent",
  high: "warning",
  critical: "danger",
};

export function RiskBadge({ risk, size = "sm", label = true }: { risk: Risk | null | undefined; size?: "xs" | "sm" | "md"; label?: boolean }) {
  if (!risk) return null;
  return (
    <Badge tone={RISK_TONE[risk] ?? "neutral"} size={size} dot>
      {label ? `${risk[0].toUpperCase()}${risk.slice(1)} risk` : risk}
    </Badge>
  );
}

/** Tiny segmented bar showing the mix of tool risks in a group. */
export function RiskMix({ risks, className }: { risks: Risk[]; className?: string }) {
  const order: Risk[] = ["low", "medium", "high", "critical"];
  const counts = order.map((r) => ({ r, n: risks.filter((x) => x === r).length })).filter((x) => x.n > 0);
  const total = risks.length || 1;
  const color: Record<Risk, string> = {
    low: "var(--success)",
    medium: "var(--accent)",
    high: "var(--warning)",
    critical: "var(--danger)",
  };
  return (
    <div className={cn("flex items-center gap-2", className)}>
      <div className="flex h-1.5 w-20 overflow-hidden rounded-full bg-surface-3" aria-hidden>
        {counts.map(({ r, n }) => (
          <span key={r} style={{ width: `${(n / total) * 100}%`, background: color[r] }} />
        ))}
      </div>
      <span className="text-[11px] text-subtle">
        {counts.map(({ r, n }) => `${n} ${r}`).join(" · ") || "no tools"}
      </span>
    </div>
  );
}

/* ---------------- Alignment ---------------- */

export const VERDICT_LABEL: Record<AlignmentVerdict, string> = {
  pass: "Pass",
  warn: "Warning",
  block: "Blocked",
  pending: "Checking",
  skipped: "Skipped",
};

export const VERDICT_TONE: Record<AlignmentVerdict, BadgeTone> = {
  pass: "success",
  warn: "warning",
  block: "danger",
  pending: "neutral",
  skipped: "outline",
};

export function VerdictBadge({ verdict, size = "sm" }: { verdict: AlignmentVerdict; size?: "xs" | "sm" | "md" }) {
  const icon =
    verdict === "pass" ? (
      <ShieldCheck />
    ) : verdict === "warn" ? (
      <ShieldAlert />
    ) : verdict === "block" ? (
      <ShieldX />
    ) : verdict === "pending" ? (
      <Loader2 className="spin" />
    ) : undefined;
  return (
    <Badge tone={VERDICT_TONE[verdict] ?? "neutral"} size={size} icon={icon}>
      {VERDICT_LABEL[verdict] ?? verdict}
    </Badge>
  );
}

export function checkerLabel(checker: string | null | undefined) {
  switch (checker) {
    case "rules":
      return "Rules";
    case "laya":
      return "Laya";
    case "judge":
      return "LLM judge";
    case "laya+judge":
      return "Laya + judge";
    case "none":
    case null:
    case undefined:
      return "—";
    default:
      return checker;
  }
}

/* ---------------- Knowledge ---------------- */

export const DOC_STATUS_LABEL: Record<DocumentStatus, string> = {
  uploaded: "Uploaded",
  parsing: "Parsing",
  structuring: "Structuring",
  embedding: "Embedding",
  questions: "Questions",
  ready: "Ready",
  failed: "Failed",
};

export const isDocProcessing = (s: DocumentStatus) => s !== "ready" && s !== "failed";

export function DocumentStatusCell({ doc }: { doc: Pick<KDocument, "status" | "progress" | "error"> }) {
  if (doc.status === "ready")
    return (
      <Badge tone="success" dot>
        Ready
      </Badge>
    );
  if (doc.status === "failed")
    return (
      <span title={doc.error ?? undefined}>
        <Badge tone="danger" dot>
          Failed
        </Badge>
      </span>
    );
  return (
    <div className="flex min-w-[140px] flex-col gap-1">
      <div className="flex items-center justify-between gap-2 text-[11.5px]">
        <span className="inline-flex items-center gap-1.5 font-medium text-fg">
          <Loader2 className="spin size-3 text-accent" />
          {DOC_STATUS_LABEL[doc.status] ?? doc.status}
        </span>
        <span className="text-subtle tabular-nums">{Math.round(doc.progress ?? 0)}%</span>
      </div>
      <ProgressBar value={doc.progress ?? 0} size="xs" />
    </div>
  );
}

const STRUCTURE_LABEL: Record<StructureSource, string> = {
  native: "Native ToC",
  headings: "Headings",
  augmented: "AI-augmented",
  flat: "Flat",
};

const STRUCTURE_TONE: Record<StructureSource, BadgeTone> = {
  native: "success",
  headings: "info",
  augmented: "accent",
  flat: "outline",
};

export function StructureBadge({ source }: { source: StructureSource | null | undefined }) {
  if (!source) return <span className="text-subtle">—</span>;
  return (
    <Badge tone={STRUCTURE_TONE[source] ?? "neutral"} size="xs">
      {STRUCTURE_LABEL[source] ?? source}
    </Badge>
  );
}

/* ---------------- Agents & roles ---------------- */

export const AUTONOMY_LABEL: Record<Autonomy, string> = {
  supervised: "Supervised",
  balanced: "Balanced",
  autonomous: "Autonomous",
};

export const AUTONOMY_HELP: Record<Autonomy, string> = {
  supervised: "Asks for approval before any medium, high or critical risk action.",
  balanced: "Acts on its own for low and medium risk; asks before high and critical risk actions.",
  autonomous: "Acts on its own; only critical-risk actions need approval.",
};

export function AutonomyPill({ autonomy, size = "xs" }: { autonomy: Autonomy; size?: "xs" | "sm" }) {
  const tone: BadgeTone = autonomy === "supervised" ? "info" : autonomy === "balanced" ? "neutral" : "accent";
  return (
    <Badge tone={tone} size={size}>
      {AUTONOMY_LABEL[autonomy] ?? autonomy}
    </Badge>
  );
}

export const ROLE_LABEL: Record<Role, string> = { owner: "Owner", admin: "Admin", member: "Member", viewer: "Viewer" };

export function RoleBadge({ role }: { role: Role }) {
  const tone: BadgeTone = role === "owner" ? "solid" : role === "admin" ? "accent" : role === "member" ? "neutral" : "outline";
  return (
    <Badge tone={tone} size="xs">
      {ROLE_LABEL[role] ?? role}
    </Badge>
  );
}
