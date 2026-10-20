"use client";

import {
  AlertTriangle,
  Brain,
  CheckCircle2,
  ChevronDown,
  CircleDot,
  CircleSlash,
  Download,
  FileBox,
  Gauge,
  GitBranch,
  Inbox,
  MessageSquareText,
  PauseCircle,
  PlayCircle,
  ShieldAlert,
  ShieldCheck,
  ShieldX,
  Timer,
  Wrench,
  XCircle,
} from "lucide-react";
import Link from "next/link";
import { useMemo, useState, type ReactNode } from "react";
import { ViewFileButton } from "@/components/files/file-link";
import { Button } from "@/components/ui/button";
import { ProgressBar } from "@/components/ui/feedback";
import { JsonView } from "@/components/ui/json-view";
import { Markdown } from "@/components/ui/markdown";
import { Timeline, TimelineItem, type TimelineTone } from "@/components/ui/timeline";
import { api, errorMessage } from "@/lib/api";
import { formatBytes, formatMs, formatTimeWithSeconds } from "@/lib/format";
import { toast } from "@/lib/toast";
import type { AlignmentVerdict, Artifact, PolicyViolation, Risk, TaskEvent, TaskStatus } from "@/lib/types";
import { cn } from "@/lib/utils";
import { checkerLabel, RiskBadge, TASK_STATUS_LABEL, VerdictBadge } from "./status";

/* eslint-disable @typescript-eslint/no-explicit-any */

export async function downloadArtifact(a: Pick<Artifact, "id" | "name">) {
  try {
    await api.download(`/artifacts/${a.id}/download`, undefined, a.name);
  } catch (err) {
    toast.error("Download failed", { description: errorMessage(err) });
  }
}

function statusVisual(status: TaskStatus): { icon: ReactNode; tone: TimelineTone } {
  switch (status) {
    case "running":
      return { icon: <PlayCircle />, tone: "accent" };
    case "completed":
      return { icon: <CheckCircle2 />, tone: "success" };
    case "failed":
      return { icon: <XCircle />, tone: "danger" };
    case "cancelled":
      return { icon: <CircleSlash />, tone: "neutral" };
    case "paused":
      return { icon: <PauseCircle />, tone: "info" };
    case "waiting_approval":
      return { icon: <ShieldAlert />, tone: "warning" };
    default:
      return { icon: <CircleDot />, tone: "neutral" };
  }
}

function Expandable({ label, children, defaultOpen = false }: { label: ReactNode; children: ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="inline-flex items-center gap-1 text-xs font-medium text-muted hover:text-fg"
      >
        <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} />
        {label}
      </button>
      {open && <div className="mt-2">{children}</div>}
    </div>
  );
}

function LongText({ text, className, mono }: { text: string; className?: string; mono?: boolean }) {
  const [full, setFull] = useState(false);
  const long = text.length > 420;
  return (
    <div className={className}>
      <p className={cn("text-[13px] leading-relaxed whitespace-pre-wrap", mono && "font-mono text-xs", !full && long && "line-clamp-5")}>{text}</p>
      {long && (
        <button type="button" onClick={() => setFull((f) => !f)} className="mt-1 text-xs font-medium text-muted hover:text-fg">
          {full ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

function Violations({ violations }: { violations: PolicyViolation[] }) {
  if (!violations?.length) return null;
  return (
    <ul className="mt-2 space-y-1.5">
      {violations.map((v, i) => (
        <li key={`${v.policy_id}-${i}`} className="rounded-md border border-border bg-surface px-2.5 py-1.5 text-xs">
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium text-fg">{v.policy_name}</span>
            <span className="font-mono text-muted">{Math.round((v.score ?? 0) * 100)}%</span>
          </div>
          {v.rule && <p className="mt-0.5 text-muted">{v.rule}</p>}
          {v.explanation && <p className="mt-0.5 text-subtle">{v.explanation}</p>}
        </li>
      ))}
    </ul>
  );
}

interface Row {
  event: TaskEvent;
  result?: TaskEvent;
}

function EventRow({ row, last }: { row: Row; last: boolean }) {
  const { event, result } = row;
  const c = (event.content ?? {}) as Record<string, any>;
  const time = formatTimeWithSeconds(event.created_at);

  switch (event.kind) {
    case "status": {
      const status = c.status as TaskStatus;
      const v = statusVisual(status);
      return (
        <TimelineItem
          last={last}
          icon={v.icon}
          tone={v.tone}
          time={time}
          title={
            <>
              Status → <span className="font-medium">{TASK_STATUS_LABEL[status] ?? String(c.status)}</span>
            </>
          }
        >
          {c.note && <p className="text-xs text-muted">{String(c.note)}</p>}
        </TimelineItem>
      );
    }
    case "thought":
      return (
        <TimelineItem last={last} icon={<Brain />} time={time} title={<span className="text-muted">Thinking</span>}>
          <LongText text={String(c.text ?? "")} className="text-muted italic" />
        </TimelineItem>
      );
    case "tool_call": {
      const ok: boolean | undefined = result ? Boolean(result.content?.ok) : undefined;
      return (
        <TimelineItem
          last={last}
          icon={<Wrench />}
          tone={ok === false ? "danger" : ok === true ? "success" : "accent"}
          time={time}
          title={
            <span className="flex flex-wrap items-center gap-2">
              <span>
                {result ? "Used" : "Calling"} <span className="font-medium">{String(c.display || c.tool)}</span>
              </span>
              {c.risk && <RiskBadge risk={c.risk as Risk} size="xs" label={false} />}
              {result && (
                <span className={cn("inline-flex items-center gap-1 text-[11.5px]", ok ? "text-success" : "text-danger")}>
                  {ok ? <CheckCircle2 className="size-3" /> : <XCircle className="size-3" />}
                  {ok ? "ok" : "failed"}
                  {result.content?.duration_ms !== undefined && (
                    <span className="inline-flex items-center gap-0.5 text-subtle">
                      <Timer className="size-3" />
                      {formatMs(Number(result.content.duration_ms))}
                    </span>
                  )}
                </span>
              )}
            </span>
          }
        >
          <div className="space-y-2">
            <Expandable label={<span className="font-mono">{String(c.tool)}</span>}>
              <div className="space-y-2">
                <p className="text-[11px] font-medium tracking-wide text-subtle uppercase">Arguments</p>
                <JsonView data={c.args ?? {}} defaultDepth={2} maxHeight={280} />
                {result && (
                  <>
                    <p className="text-[11px] font-medium tracking-wide text-subtle uppercase">Result</p>
                    <pre className="max-h-72 overflow-auto rounded-lg border border-border bg-[var(--code-bg)] p-3 font-mono text-[11.5px] leading-relaxed whitespace-pre-wrap text-muted">
                      {String(result.content?.preview ?? "(no preview)")}
                    </pre>
                  </>
                )}
              </div>
            </Expandable>
          </div>
        </TimelineItem>
      );
    }
    case "tool_result": {
      const ok = Boolean(c.ok);
      return (
        <TimelineItem
          last={last}
          icon={ok ? <CheckCircle2 /> : <XCircle />}
          tone={ok ? "success" : "danger"}
          time={time}
          title={
            <>
              Result from <span className="font-medium">{String(c.tool)}</span>
              {c.duration_ms !== undefined && <span className="text-subtle"> · {formatMs(Number(c.duration_ms))}</span>}
            </>
          }
        >
          {c.preview && (
            <pre className="max-h-60 overflow-auto rounded-lg border border-border bg-[var(--code-bg)] p-3 font-mono text-[11.5px] whitespace-pre-wrap text-muted">
              {String(c.preview)}
            </pre>
          )}
        </TimelineItem>
      );
    }
    case "message":
      return (
        <TimelineItem last={last} icon={<MessageSquareText />} tone="info" time={time} title={<span className="font-medium">Agent message</span>}>
          <div className="rounded-card border border-border bg-surface px-3.5 py-2.5">
            <Markdown content={String(c.text ?? "")} size="sm" />
          </div>
        </TimelineItem>
      );
    case "progress": {
      const pct = Number(c.progress ?? 0);
      return (
        <TimelineItem
          last={last}
          icon={<Gauge />}
          time={time}
          title={
            <>
              Progress <span className="font-medium tabular-nums">{Math.round(pct)}%</span>
              {c.note && <span className="text-muted"> — {String(c.note)}</span>}
            </>
          }
        >
          <ProgressBar value={pct} size="xs" className="max-w-xs" />
        </TimelineItem>
      );
    }
    case "inbox": {
      const from = String(c.from ?? "user");
      const who = c.sender_name ? String(c.sender_name) : from === "orchestrator" ? "Ren" : from === "agent" ? "Another agent" : "You";
      return (
        <TimelineItem
          last={last}
          icon={<Inbox />}
          tone="accent"
          time={time}
          title={
            <>
              Message from <span className="font-medium">{who}</span>
            </>
          }
        >
          <div className="rounded-2xl rounded-tl-md bg-surface-2 px-3.5 py-2 text-[13px] whitespace-pre-wrap text-fg">{String(c.text ?? "")}</div>
        </TimelineItem>
      );
    }
    case "policy": {
      const verdict = (c.verdict ?? "pass") as AlignmentVerdict;
      const action = String(c.action ?? "");
      const icon = verdict === "block" ? <ShieldX /> : verdict === "warn" ? <ShieldAlert /> : <ShieldCheck />;
      const tone: TimelineTone = verdict === "block" ? "danger" : verdict === "warn" ? "warning" : "success";
      return (
        <TimelineItem
          last={last}
          icon={icon}
          tone={tone}
          time={time}
          title={
            <span className="flex flex-wrap items-center gap-2">
              Policy check <VerdictBadge verdict={verdict} size="xs" />
              {action && <span className="text-xs text-muted">{action}</span>}
              {c.checker && <span className="text-xs text-subtle">· {checkerLabel(String(c.checker))}</span>}
            </span>
          }
        >
          <Violations violations={(c.violations ?? []) as PolicyViolation[]} />
        </TimelineItem>
      );
    }
    case "delegation":
      return (
        <TimelineItem
          last={last}
          icon={<GitBranch />}
          tone="accent"
          time={time}
          title={
            <>
              Delegated to <span className="font-medium">{String(c.agent_name ?? "an agent")}</span>
            </>
          }
        >
          {c.child_task_id ? (
            <Link
              href={`/tasks/${c.child_task_id}`}
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 py-1.5 text-[13px] text-fg hover:border-border-strong"
            >
              {String(c.title ?? "Sub-task")} →
            </Link>
          ) : (
            <p className="text-[13px] text-muted">{String(c.title ?? "")}</p>
          )}
        </TimelineItem>
      );
    case "error":
      return (
        <TimelineItem last={last} icon={<AlertTriangle />} tone="danger" time={time} title={<span className="font-medium text-danger">Error</span>}>
          <LongText
            text={String(c.message ?? "Unknown error")}
            mono
            className="rounded-lg border border-[color-mix(in_srgb,var(--danger)_25%,transparent)] bg-[color-mix(in_srgb,var(--danger)_5%,transparent)] px-3 py-2 text-danger"
          />
        </TimelineItem>
      );
    case "artifact": {
      const a = (c.artifact ?? {}) as Artifact;
      return (
        <TimelineItem
          last={last}
          icon={<FileBox />}
          tone="info"
          time={time}
          title={
            <>
              Produced <span className="font-medium">{a.name ?? "a file"}</span>
            </>
          }
        >
          {a.id && (
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
              <span>
                {a.mime} · {formatBytes(a.size)}
              </span>
              <ViewFileButton source={{ artifactId: a.id }} name={a.name} />
              <Button size="xs" variant="secondary" icon={<Download className="size-3" />} onClick={() => void downloadArtifact(a)}>
                Download
              </Button>
            </div>
          )}
        </TimelineItem>
      );
    }
    default:
      return (
        <TimelineItem last={last} icon={<CircleDot />} time={time} title={<span className="text-muted">{String(event.kind)}</span>}>
          <JsonView data={event.content} defaultDepth={1} />
        </TimelineItem>
      );
  }
}

/** Vertical timeline of task events. Tool calls are paired with their results by `call_id`. */
export function TaskTimeline({ events, className }: { events: TaskEvent[]; className?: string }) {
  const rows = useMemo<Row[]>(() => {
    const sorted = events.slice().sort((a, b) => a.created_at.localeCompare(b.created_at));
    const results = new Map<string, TaskEvent>();
    for (const e of sorted) {
      if (e.kind === "tool_result" && e.content?.call_id) results.set(String(e.content.call_id), e);
    }
    const paired = new Set<string>();
    const out: Row[] = [];
    for (const e of sorted) {
      if (e.kind === "tool_call") {
        const r = e.content?.call_id ? results.get(String(e.content.call_id)) : undefined;
        if (r) paired.add(r.id);
        out.push({ event: e, result: r });
      } else if (e.kind === "tool_result" && paired.has(e.id)) {
        continue;
      } else {
        out.push({ event: e });
      }
    }
    // A result that arrived before its call is rendered on its own; drop it if it got paired later.
    return out.filter((r) => !(r.event.kind === "tool_result" && paired.has(r.event.id)));
  }, [events]);

  if (rows.length === 0) {
    return <p className={cn("py-8 text-center text-[13px] text-muted", className)}>No activity yet. Events stream in as the agent works.</p>;
  }
  return (
    <Timeline className={className}>
      {rows.map((row, i) => (
        <EventRow key={row.event.id} row={row} last={i === rows.length - 1} />
      ))}
    </Timeline>
  );
}
