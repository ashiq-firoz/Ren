"use client";

import { Loader2, ShieldAlert, ShieldCheck, ShieldX } from "lucide-react";
import { checkerLabel } from "@/components/domain/status";
import { Popover } from "@/components/ui/floating";
import { formatMs } from "@/lib/format";
import type { AlignmentSummary } from "@/lib/types";
import { cn } from "@/lib/utils";

function Violations({ summary }: { summary: AlignmentSummary }) {
  return (
    <div className="w-[min(340px,calc(100vw-2rem))] p-3.5">
      <p className="text-[13px] font-semibold text-fg">
        {summary.verdict === "block" ? (summary.revised ? "Revised to comply" : "Blocked by policy") : summary.verdict === "warn" ? "Policy warning" : "Policy check passed"}
      </p>
      <p className="mt-0.5 text-xs text-muted">
        Checked by {checkerLabel(summary.checker)}
        {summary.latency_ms !== null && summary.latency_ms !== undefined ? ` in ${formatMs(summary.latency_ms)}` : ""}.
        {summary.revised && " The original reply violated a blocking policy, so Ren rewrote it."}
      </p>
      {summary.violations.length > 0 && (
        <ul className="mt-3 space-y-2.5">
          {summary.violations.map((v, i) => (
            <li key={`${v.policy_id}-${i}`} className="rounded-md border border-border bg-surface-2/60 p-2.5">
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-xs font-semibold text-fg">{v.policy_name}</span>
                <span className="shrink-0 font-mono text-[11px] text-muted">{Math.round(v.score * 100)}%</span>
              </div>
              {v.rule && <p className="mt-1 text-xs text-muted">{v.rule}</p>}
              <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-surface-3">
                <div
                  className={cn("h-full rounded-full", v.score >= 0.7 ? "bg-danger" : v.score >= 0.45 ? "bg-warning" : "bg-success")}
                  style={{ width: `${Math.min(100, Math.max(2, v.score * 100))}%` }}
                />
              </div>
              {v.explanation && <p className="mt-1.5 text-[11.5px] leading-snug text-subtle">{v.explanation}</p>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Subtle policy-check indicator under assistant messages. */
export function AlignmentBadge({ summary, className }: { summary: AlignmentSummary | undefined; className?: string }) {
  if (!summary || summary.verdict === "skipped") return null;
  const base = "inline-flex h-6 items-center gap-1.5 rounded-full px-2 text-[11.5px] transition-colors";
  if (summary.verdict === "pending") {
    return (
      <span className={cn(base, "text-subtle", className)} aria-live="polite">
        <Loader2 className="spin size-3" /> Checking policies…
      </span>
    );
  }
  const latency = summary.latency_ms !== null && summary.latency_ms !== undefined ? formatMs(summary.latency_ms) : null;
  const checker = checkerLabel(summary.checker);
  const content =
    summary.verdict === "pass" ? (
      <>
        <ShieldCheck className="size-3.5 text-success" />
        <span>Policy check passed</span>
        {latency && <span className="text-subtle">· {latency}</span>}
        {checker !== "—" && <span className="text-subtle">· {checker}</span>}
      </>
    ) : summary.verdict === "warn" ? (
      <>
        <ShieldAlert className="size-3.5 text-warning" />
        <span className="text-warning">Warning</span>
        <span className="text-subtle">
          · {summary.violations[0]?.policy_name ?? "policy"}
          {summary.violations.length > 1 ? ` +${summary.violations.length - 1}` : ""}
        </span>
      </>
    ) : (
      <>
        <ShieldX className="size-3.5 text-danger" />
        <span className="text-danger">{summary.revised ? "Revised to comply" : "Blocked by policy"}</span>
        {summary.violations[0] && <span className="text-subtle">· {summary.violations[0].policy_name}</span>}
      </>
    );
  return (
    <Popover
      hover
      placement="top-start"
      aria-label="Policy check details"
      trigger={
        <button type="button" className={cn(base, "text-muted hover:bg-surface-2", className)}>
          {content}
        </button>
      }
    >
      <Violations summary={summary} />
    </Popover>
  );
}
