"use client";

import { BookOpen, FileText } from "lucide-react";
import Link from "next/link";
import { Popover } from "@/components/ui/floating";
import type { Citation } from "@/lib/types";
import { cn } from "@/lib/utils";

export function formatBreadcrumb(b: string | null | undefined) {
  if (!b) return null;
  return b
    .split(/\s*(?:>|›|\/)\s*/)
    .filter(Boolean)
    .join(" › ");
}

export function citationHref(c: Pick<Citation, "document_id" | "section_id">) {
  return `/knowledge/${c.document_id}${c.section_id ? `?section=${encodeURIComponent(c.section_id)}` : ""}`;
}

function CitationCard({ citation, n }: { citation?: Citation; n: number }) {
  if (!citation) {
    return <div className="p-3 text-xs text-muted">Source [{n}] is not available.</div>;
  }
  const crumb = formatBreadcrumb(citation.breadcrumb);
  return (
    <div className="w-[min(360px,calc(100vw-2rem))] p-3.5">
      <div className="flex items-start gap-2.5">
        <span className="mt-0.5 inline-flex size-6 shrink-0 items-center justify-center rounded-md bg-surface-2 text-[11px] font-semibold text-muted">
          {n}
        </span>
        <div className="min-w-0">
          <Link
            href={citationHref(citation)}
            className="line-clamp-2 text-[13px] font-semibold text-fg hover:underline hover:underline-offset-2"
          >
            {citation.document_title}
          </Link>
          {crumb && <p className="mt-0.5 line-clamp-2 text-xs text-muted">{crumb}</p>}
          {(citation.section_path || citation.section_title) && (
            <p className="mt-1 text-[11.5px] text-subtle">
              {citation.section_path && <span className="font-mono">§{citation.section_path}</span>}
              {citation.section_path && citation.section_title ? " · " : ""}
              {citation.section_title}
            </p>
          )}
        </div>
      </div>
      {citation.snippet && (
        <blockquote className="mt-2.5 line-clamp-6 border-l-2 border-border-strong pl-2.5 text-[12.5px] leading-relaxed text-muted">
          {citation.snippet}
        </blockquote>
      )}
      <div className="mt-2.5 flex items-center justify-between text-[11px] text-subtle">
        <span>{citation.score !== null && citation.score !== undefined ? `Relevance ${citation.score.toFixed(2)}` : ""}</span>
        <Link href={citationHref(citation)} className="inline-flex items-center gap-1 font-medium text-muted hover:text-fg">
          <BookOpen className="size-3" /> Open section
        </Link>
      </div>
    </div>
  );
}

/** Superscript `[n]` pill with a hover/click popover showing the cited section. */
export function CitationChip({ n, citation }: { n: number; citation?: Citation }) {
  return (
    <Popover
      hover
      placement="top-start"
      aria-label={`Source ${n}`}
      trigger={
        <button
          type="button"
          aria-label={`Source ${n}${citation ? `: ${citation.document_title}` : ""}`}
          className={cn(
            "mx-[1px] inline-flex h-[17px] min-w-[17px] -translate-y-[0.35em] items-center justify-center rounded-full border border-border-strong bg-surface-2 px-1 align-baseline font-sans text-[10px] leading-none font-semibold text-muted transition-colors hover:border-accent hover:bg-surface-3 hover:text-fg",
          )}
        >
          {n}
        </button>
      }
    >
      <CitationCard citation={citation} n={n} />
    </Popover>
  );
}

/** "Sources" row listing the cited documents under a message. */
export function SourcesRow({ citations, className }: { citations: Citation[]; className?: string }) {
  if (!citations.length) return null;
  const sorted = citations.slice().sort((a, b) => a.id - b.id);
  return (
    <div className={cn("flex flex-wrap items-center gap-1.5", className)}>
      <span className="mr-0.5 text-[11px] font-medium tracking-wide text-subtle uppercase">Sources</span>
      {sorted.map((c) => (
        <Popover
          key={c.id}
          hover
          placement="top-start"
          trigger={
            <button
              type="button"
              className="inline-flex h-6 max-w-[260px] items-center gap-1.5 rounded-full border border-border bg-surface px-2 text-[11.5px] text-muted transition-colors hover:border-border-strong hover:text-fg"
            >
              <span className="font-semibold text-fg">{c.id}</span>
              <FileText className="size-3 shrink-0" />
              <span className="truncate">{c.document_title}</span>
              {c.section_path && <span className="shrink-0 font-mono text-[10.5px] text-subtle">§{c.section_path}</span>}
            </button>
          }
        >
          <CitationCard citation={c} n={c.id} />
        </Popover>
      ))}
    </div>
  );
}
