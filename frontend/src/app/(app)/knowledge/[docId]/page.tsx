"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ChevronRight, CircleDot, Download, FileText, HelpCircle, ListTree, Loader2, RefreshCw, Search } from "lucide-react";
import Link from "next/link";
import { useParams, usePathname, useRouter, useSearchParams } from "next/navigation";
import { useMemo, useState } from "react";
import { DOC_STATUS_LABEL, isDocProcessing, StructureBadge } from "@/components/domain/status";
import { formatBreadcrumb } from "@/components/domain/citations";
import { MobileNavButton } from "@/components/shell/page-header";
import { NotificationBell } from "@/components/shell/notification-bell";
import { Badge } from "@/components/ui/badge";
import { Button, ButtonLink } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, ErrorState, Notice, ProgressBar, Skeleton, SkeletonText } from "@/components/ui/feedback";
import { Input } from "@/components/ui/input";
import { KeyValue } from "@/components/ui/key-value";
import { Markdown } from "@/components/ui/markdown";
import { useDocument, useSection } from "@/hooks/queries";
import { api, ApiError, errorMessage } from "@/lib/api";
import { upsertDocument } from "@/lib/cache";
import { formatBytes, formatDate, formatNumber } from "@/lib/format";
import { toast } from "@/lib/toast";
import type { KDocument, TocNode } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useRole } from "@/providers/auth";

function firstLeaf(nodes: TocNode[]): TocNode | null {
  for (const n of nodes) {
    if (n.is_leaf || n.children.length === 0) return n;
    const leaf = firstLeaf(n.children);
    if (leaf) return leaf;
  }
  return null;
}

function ancestorsOf(nodes: TocNode[], id: string, trail: string[] = []): string[] | null {
  for (const n of nodes) {
    if (n.id === id) return trail;
    const found = ancestorsOf(n.children, id, [...trail, n.id]);
    if (found) return found;
  }
  return null;
}

function matches(node: TocNode, q: string): boolean {
  return node.title.toLowerCase().includes(q) || node.path.includes(q) || node.children.some((c) => matches(c, q));
}

function TocItem({
  node,
  depth,
  selectedId,
  expanded,
  onToggle,
  onSelect,
  filter,
}: {
  node: TocNode;
  depth: number;
  selectedId: string | null;
  expanded: Set<string>;
  onToggle: (id: string) => void;
  onSelect: (id: string) => void;
  filter: string;
}) {
  if (filter && !matches(node, filter)) return null;
  const hasChildren = node.children.length > 0;
  const open = filter ? true : expanded.has(node.id);
  const selected = node.id === selectedId;
  return (
    <li>
      <div
        className={cn(
          "group flex items-center gap-1 rounded-md pr-2 transition-colors",
          selected ? "bg-surface-2" : "hover:bg-surface-2/60",
        )}
        style={{ paddingLeft: depth * 14 + 4 }}
      >
        {hasChildren ? (
          <button
            type="button"
            aria-label={open ? `Collapse ${node.title}` : `Expand ${node.title}`}
            aria-expanded={open}
            onClick={() => onToggle(node.id)}
            className="flex size-5 shrink-0 items-center justify-center rounded text-subtle hover:text-fg"
          >
            <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} />
          </button>
        ) : (
          <span className="flex size-5 shrink-0 items-center justify-center" aria-hidden>
            <CircleDot className={cn("size-2.5", node.is_leaf ? "text-accent" : "text-subtle")} />
          </span>
        )}
        <button
          type="button"
          onClick={() => onSelect(node.id)}
          aria-current={selected ? "true" : undefined}
          className="flex min-w-0 flex-1 items-baseline gap-2 py-1.5 text-left"
        >
          <span className="shrink-0 font-mono text-[10.5px] text-subtle">{node.path}</span>
          <span className={cn("truncate text-[12.5px]", selected ? "font-medium text-fg" : "text-fg")}>{node.title}</span>
          <span className="ml-auto shrink-0 text-[10.5px] text-subtle tabular-nums opacity-0 group-hover:opacity-100">
            {formatNumber(node.token_count, { compact: true })} tok
          </span>
        </button>
      </div>
      {hasChildren && open && (
        <ul>
          {node.children.map((c) => (
            <TocItem
              key={c.id}
              node={c}
              depth={depth + 1}
              selectedId={selectedId}
              expanded={expanded}
              onToggle={onToggle}
              onSelect={onSelect}
              filter={filter}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

function SectionPane({ docId, sectionId, onSelect }: { docId: string; sectionId: string | null; onSelect: (id: string) => void }) {
  const { data: section, isLoading, error, refetch } = useSection(docId, sectionId);
  if (!sectionId) {
    return <EmptyState compact icon={<ListTree />} title="Pick a section" description="Select a node in the table of contents to read it." />;
  }
  if (isLoading) {
    return (
      <div className="space-y-4 p-6">
        <Skeleton className="h-3 w-1/2" />
        <Skeleton className="h-7 w-2/3" />
        <SkeletonText lines={8} className="mt-6" />
      </div>
    );
  }
  if (error || !section) return <ErrorState error={error ?? new Error("Section unavailable")} onRetry={() => refetch()} />;
  const crumb = formatBreadcrumb(section.breadcrumb);
  return (
    <article className="p-5 sm:p-7">
      {crumb && <p className="text-xs text-muted">{crumb}</p>}
      <div className="mt-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="font-serif-display text-[28px] leading-tight text-fg">{section.title}</h2>
        <span className="font-mono text-xs text-subtle">§{section.path}</span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-[11.5px] text-muted">
        <Badge tone={section.is_leaf ? "accent" : "neutral"} size="xs">
          {section.is_leaf ? "Leaf section" : `Level ${section.level}`}
        </Badge>
        <span>{formatNumber(section.token_count)} tokens</span>
        {section.page_start !== null && (
          <span>
            · page {section.page_start}
            {section.page_end !== null && section.page_end !== section.page_start ? `–${section.page_end}` : ""}
          </span>
        )}
      </div>

      {section.summary && (
        <div className="mt-5 rounded-card border border-border bg-surface-2/50 px-4 py-3">
          <p className="mb-1 text-[11px] font-medium tracking-wide text-subtle uppercase">Summary</p>
          <p className="text-[13.5px] leading-relaxed text-fg">{section.summary}</p>
        </div>
      )}

      <div className="mt-6">
        {section.content ? <Markdown content={section.content} /> : <p className="text-[13px] text-muted">This section has no body text of its own.</p>}
      </div>

      {section.children.length > 0 && (
        <div className="mt-8">
          <p className="mb-2 text-[11px] font-medium tracking-wide text-subtle uppercase">Subsections</p>
          <ul className="space-y-1">
            {section.children.map((c) => (
              <li key={c.id}>
                <button
                  type="button"
                  onClick={() => onSelect(c.id)}
                  className="flex w-full items-baseline gap-2 rounded-md px-2 py-1.5 text-left hover:bg-surface-2"
                >
                  <span className="font-mono text-[11px] text-subtle">{c.path}</span>
                  <span className="text-[13px] text-fg">{c.title}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-8">
        <p className="mb-2 flex items-center gap-1.5 text-[11px] font-medium tracking-wide text-subtle uppercase">
          <HelpCircle className="size-3.5" /> STAIR training questions
        </p>
        {section.questions.length === 0 ? (
          <p className="text-[13px] text-muted">No synthetic questions were generated for this section.</p>
        ) : (
          <ul className="space-y-1.5">
            {section.questions.map((qq, i) => (
              <li key={i} className="rounded-md border border-border bg-surface px-3 py-2 text-[13px] text-fg">
                {qq}
              </li>
            ))}
          </ul>
        )}
      </div>
    </article>
  );
}

export default function DocumentPage() {
  const { docId } = useParams<{ docId: string }>();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const qc = useQueryClient();
  const { canWrite } = useRole();
  const { data: doc, isLoading, error, refetch } = useDocument(docId);
  const [filter, setFilter] = useState("");
  const [userExpanded, setUserExpanded] = useState<Map<string, boolean>>(new Map());

  const toc = useMemo(() => doc?.toc ?? [], [doc?.toc]);
  const selectedId = params.get("section") ?? firstLeaf(toc)?.id ?? null;

  // Default expansion: top level + the path to the selected node; user toggles override.
  const expanded = useMemo(() => {
    const set = new Set<string>(toc.map((n) => n.id));
    if (selectedId) for (const id of ancestorsOf(toc, selectedId) ?? []) set.add(id);
    for (const [id, open] of userExpanded) {
      if (open) set.add(id);
      else set.delete(id);
    }
    return set;
  }, [toc, selectedId, userExpanded]);

  const select = (id: string) => router.replace(`${pathname}?section=${encodeURIComponent(id)}`, { scroll: false });
  const toggle = (id: string) =>
    setUserExpanded((m) => {
      const next = new Map(m);
      next.set(id, !expanded.has(id));
      return next;
    });

  const reindex = useMutation({
    mutationFn: () => api.post<KDocument>(`/documents/${docId}/reindex`),
    onSuccess: (d) => {
      upsertDocument(qc, d);
      toast.info("Reindexing started");
    },
    meta: { errorTitle: "Couldn't reindex" },
  });

  if (isLoading) {
    return (
      <div className="mx-auto w-full max-w-[1440px] px-4 pt-6 sm:px-6 lg:px-8">
        <Skeleton className="h-4 w-28" />
        <Skeleton className="mt-4 h-9 w-1/2" />
        <Skeleton className="mt-6 h-20 w-full rounded-card" />
        <div className="mt-6 grid gap-6 lg:grid-cols-[340px_1fr]">
          <Skeleton className="h-[480px] rounded-card" />
          <Skeleton className="h-[480px] rounded-card" />
        </div>
      </div>
    );
  }

  if (error || !doc) {
    const notFound = error instanceof ApiError && (error.status === 404 || error.status === 403);
    return (
      <div className="mx-auto w-full max-w-[1200px] px-4 pt-6 sm:px-6 lg:px-8">
        <Card>
          {notFound ? (
            <EmptyState title="Document not found" description="It may have been deleted, or you don't have access to its collection." action={<ButtonLink href="/knowledge">Back to knowledge</ButtonLink>} />
          ) : (
            <ErrorState error={error} onRetry={() => refetch()} />
          )}
        </Card>
      </div>
    );
  }

  const processing = isDocProcessing(doc.status);

  return (
    <div className="mx-auto flex w-full max-w-[1440px] flex-col px-4 pt-6 pb-10 sm:px-6 lg:px-8">
      <div className="mb-4 flex items-center justify-between">
        <div className="flex items-center gap-1">
          <MobileNavButton className="-ml-1.5" />
          <Link href={`/knowledge?collection=${doc.collection_id}`} className="inline-flex items-center gap-1.5 text-[13px] text-muted hover:text-fg">
            <ArrowLeft className="size-3.5" /> {doc.collection_name}
          </Link>
        </div>
        <NotificationBell />
      </div>
      <header className="mb-5 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="font-serif-display text-[30px] leading-tight text-fg sm:text-[34px]">{doc.title}</h1>
          <p className="mt-1 flex flex-wrap items-center gap-2 text-[13px] text-muted">
            <FileText className="size-3.5" /> {doc.filename} · {formatBytes(doc.size)}
            <StructureBadge source={doc.structure_source} />
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            icon={<Download className="size-4" />}
            onClick={async () => {
              try {
                await api.download(`/documents/${doc.id}/download`, undefined, doc.filename);
              } catch (err) {
                toast.error("Download failed", { description: errorMessage(err) });
              }
            }}
          >
            Download
          </Button>
          {canWrite && (
            <Button icon={<RefreshCw className="size-4" />} loading={reindex.isPending} disabled={processing} onClick={() => reindex.mutate()}>
              Reindex
            </Button>
          )}
        </div>
      </header>

      <Card className="mb-5 px-5 py-4">
        <KeyValue
          columns={4}
          dense
          items={[
            { label: "Pages", value: doc.page_count ?? "—" },
            { label: "Sections", value: formatNumber(doc.section_count) },
            { label: "Leaves", value: formatNumber(doc.leaf_count) },
            { label: "Chunks", value: formatNumber(doc.chunk_count) },
            { label: "Questions", value: formatNumber(doc.question_count) },
            { label: "Status", value: DOC_STATUS_LABEL[doc.status] ?? doc.status },
            { label: "Uploaded by", value: doc.uploaded_by_name ?? "—" },
            { label: "Uploaded", value: formatDate(doc.created_at) },
          ]}
        />
      </Card>

      {processing && (
        <Notice tone="info" icon={<Loader2 className="spin" />} className="mb-5" title={`${DOC_STATUS_LABEL[doc.status]}… ${Math.round(doc.progress)}%`}>
          <ProgressBar value={doc.progress} size="xs" className="mt-2 max-w-md" />
          <p className="mt-2">The structure appears here as soon as indexing finishes — this page updates live.</p>
        </Notice>
      )}
      {doc.status === "failed" && (
        <Notice tone="danger" className="mb-5" title="Indexing failed">
          {doc.error ?? "The document could not be processed."}
        </Notice>
      )}

      {toc.length === 0 ? (
        !processing && (
          <Card>
            <EmptyState compact icon={<ListTree />} title="No structure" description="This document has no table of contents yet." />
          </Card>
        )
      ) : (
        <div className="grid min-h-0 gap-5 lg:grid-cols-[340px_minmax(0,1fr)]">
          <Card className="flex max-h-[calc(100dvh-140px)] flex-col overflow-hidden lg:sticky lg:top-4">
            <div className="border-b border-border p-3">
              <Input inputSize="sm" leftIcon={<Search />} value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter sections" aria-label="Filter sections" />
            </div>
            <nav aria-label="Table of contents" className="min-h-0 flex-1 overflow-y-auto p-2">
              <ul>
                {toc.map((n) => (
                  <TocItem
                    key={n.id}
                    node={n}
                    depth={0}
                    selectedId={selectedId}
                    expanded={expanded}
                    onToggle={toggle}
                    onSelect={select}
                    filter={filter.trim().toLowerCase()}
                  />
                ))}
              </ul>
            </nav>
            <div className="flex items-center gap-3 border-t border-border px-3 py-2 text-[11px] text-subtle">
              <span className="inline-flex items-center gap-1">
                <CircleDot className="size-2.5 text-accent" /> leaf (retrievable unit)
              </span>
            </div>
          </Card>
          <Card className="min-w-0">
            <SectionPane docId={doc.id} sectionId={selectedId} onSelect={select} />
          </Card>
        </div>
      )}
    </div>
  );
}
