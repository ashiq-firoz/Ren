"use client";

import { Check, ChevronDown, Loader2, Wrench, X } from "lucide-react";
import { useState } from "react";
import { JsonView } from "@/components/ui/json-view";
import type { ToolCall } from "@/lib/types";
import { cn } from "@/lib/utils";

function ToolChip({ call }: { call: ToolCall }) {
  const [open, setOpen] = useState(false);
  const hasDetail = call.args !== undefined || !!call.result_preview;
  return (
    <div className={cn("max-w-full", open && "w-full")}>
      <button
        type="button"
        onClick={() => hasDetail && setOpen((o) => !o)}
        aria-expanded={hasDetail ? open : undefined}
        className={cn(
          "inline-flex h-7 max-w-full items-center gap-1.5 rounded-full border px-2.5 text-[12px] transition-colors",
          call.status === "running"
            ? "border-[color-mix(in_srgb,var(--accent)_45%,transparent)] bg-[color-mix(in_srgb,var(--accent)_9%,transparent)] text-fg"
            : call.status === "error"
              ? "border-[color-mix(in_srgb,var(--danger)_35%,transparent)] text-danger"
              : "border-border bg-surface text-muted hover:text-fg",
        )}
      >
        {call.status === "running" ? (
          <Loader2 className="spin size-3.5 text-accent" />
        ) : call.status === "error" ? (
          <X className="size-3.5" />
        ) : (
          <Check className="size-3.5 text-success" />
        )}
        <span className="truncate">
          {call.display || call.name}
          {call.status === "running" && !/…|\.\.\.$/.test(call.display || "") ? "…" : ""}
        </span>
        {hasDetail && <ChevronDown className={cn("size-3 shrink-0 transition-transform", open && "rotate-180")} />}
      </button>
      {open && (
        <div className="mt-1.5 mb-1 space-y-2 rounded-card border border-border bg-surface p-3 text-[12px]">
          <div className="flex items-center gap-1.5 text-muted">
            <Wrench className="size-3.5" />
            <span className="font-mono">{call.name}</span>
          </div>
          {call.args !== undefined && call.args !== null && <JsonView data={call.args} defaultDepth={1} maxHeight={220} />}
          {call.result_preview && (
            <pre className="max-h-48 overflow-auto rounded-md bg-[var(--code-bg)] p-2.5 font-mono text-[11.5px] whitespace-pre-wrap text-muted">
              {call.result_preview}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

export function ToolCallChips({ calls, className }: { calls: ToolCall[]; className?: string }) {
  if (!calls.length) return null;
  return (
    <div className={cn("flex flex-wrap items-start gap-1.5", className)}>
      {calls.map((c) => (
        <ToolChip key={c.id} call={c} />
      ))}
    </div>
  );
}
