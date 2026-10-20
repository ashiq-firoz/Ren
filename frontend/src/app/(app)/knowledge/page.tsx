"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  BookOpen,
  Download,
  FileText,
  FlaskConical,
  Layers,
  Lock,
  MoreHorizontal,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { CollectionDialog } from "@/components/knowledge/collection-dialog";
import { DocumentStatusCell, StructureBadge } from "@/components/domain/status";
import { PageContainer, PageHeader } from "@/components/shell/page-header";
import { Badge } from "@/components/ui/badge";
import { Button, ButtonLink, IconButton } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { FileDropzone } from "@/components/ui/dropzone";
import { EmptyState, ErrorState, ProgressBar, Skeleton, SkeletonRows } from "@/components/ui/feedback";
import { DropdownMenu } from "@/components/ui/floating";
import { Input, Select } from "@/components/ui/input";
import { confirm } from "@/components/ui/overlay";
import { Table, TableContainer, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useCollections, useDocuments, useKnowledgeStats } from "@/hooks/queries";
import { useDebounced } from "@/hooks/use-browser";
import { api, errorMessage } from "@/lib/api";
import { removeDocument, upsertDocument } from "@/lib/cache";
import { formatBytes, formatDate, formatNumber, timeAgo } from "@/lib/format";
import { qk } from "@/lib/query-keys";
import { toast } from "@/lib/toast";
import type { Collection, KDocument } from "@/lib/types";
import { cn, uid } from "@/lib/utils";
import { useRole } from "@/providers/auth";

const ACCEPT = ".pdf,.docx,.doc,.md,.markdown,.txt,.html,.htm,.xlsx,.xls,.csv,.pptx";

interface UploadRow {
  id: string;
  name: string;
  size: number;
  progress: number;
  status: "uploading" | "error";
  error?: string;
  controller: AbortController;
}

function StatsStrip() {
  const { data, isLoading } = useKnowledgeStats();
  const items: [string, string][] = data
    ? [
        ["Documents", formatNumber(data.documents)],
        ["Sections", formatNumber(data.sections)],
        ["Leaves", formatNumber(data.leaves)],
        ["Chunks", formatNumber(data.chunks)],
        ["Questions", formatNumber(data.questions)],
      ]
    : [];
  return (
    <Card className="mb-5 flex flex-wrap items-center gap-x-8 gap-y-3 px-5 py-3.5">
      {isLoading || !data ? (
        <Skeleton className="h-8 w-full" />
      ) : (
        <>
          {items.map(([label, value]) => (
            <div key={label}>
              <p className="text-[11px] font-medium tracking-wide text-subtle uppercase">{label}</p>
              <p className="text-[17px] font-semibold text-fg">{value}</p>
            </div>
          ))}
          <div className="ml-auto text-right text-xs text-muted">
            <p>
              Embeddings <span className="font-mono text-fg">{data.embedding_model}</span> · {data.embedding_dim}d
            </p>
            <p>
              Reranker <span className="font-mono text-fg">{data.reranker ?? "off"}</span>
            </p>
          </div>
        </>
      )}
    </Card>
  );
}

function DocumentActions({ doc, canWrite }: { doc: KDocument; canWrite: boolean }) {
  const qc = useQueryClient();
  const reindex = useMutation({
    mutationFn: () => api.post<KDocument>(`/documents/${doc.id}/reindex`),
    onSuccess: (d) => {
      upsertDocument(qc, d);
      toast.info("Reindexing started", { description: d.title });
    },
    meta: { errorTitle: "Couldn't reindex" },
  });
  const remove = useMutation({
    mutationFn: () => api.delete(`/documents/${doc.id}`),
    onSuccess: () => {
      removeDocument(qc, doc.id);
      void qc.invalidateQueries({ queryKey: qk.collections });
      void qc.invalidateQueries({ queryKey: qk.knowledgeStats });
      toast.success("Document deleted", { description: doc.title });
    },
    meta: { errorTitle: "Couldn't delete the document" },
  });
  const download = async () => {
    try {
      await api.download(`/documents/${doc.id}/download`, undefined, doc.filename);
    } catch (err) {
      toast.error("Download failed", { description: errorMessage(err) });
    }
  };
  return (
    <DropdownMenu
      aria-label={`Actions for ${doc.title}`}
      items={[
        { label: "Open structure", icon: <BookOpen />, href: `/knowledge/${doc.id}` },
        { label: "Download original", icon: <Download />, onSelect: () => void download() },
        { label: "Reindex", icon: <RefreshCw />, disabled: !canWrite, onSelect: () => reindex.mutate() },
        { type: "separator" },
        {
          label: "Delete",
          icon: <Trash2 />,
          danger: true,
          disabled: !canWrite,
          onSelect: async () => {
            const ok = await confirm({
              title: "Delete document?",
              description: `“${doc.title}” and its sections, chunks and questions will be removed from the knowledge base.`,
              confirmLabel: "Delete",
              danger: true,
            });
            if (ok) remove.mutate();
          },
        },
      ]}
      trigger={<IconButton label="Document actions" icon={<MoreHorizontal />} onClick={(e) => e.stopPropagation()} />}
    />
  );
}

function CollectionsNav({
  collections,
  selected,
  onSelect,
  onNew,
  onEdit,
  onDelete,
  isAdmin,
  totalDocs,
}: {
  collections: Collection[];
  selected: string;
  onSelect: (id: string) => void;
  onNew: () => void;
  onEdit: (c: Collection) => void;
  onDelete: (c: Collection) => void;
  isAdmin: boolean;
  totalDocs: number;
}) {
  const row = (id: string, label: string, count: number, extra?: React.ReactNode, c?: Collection) => (
    <div
      key={id}
      className={cn(
        "group flex items-center rounded-control transition-colors",
        selected === id ? "bg-surface-2" : "hover:bg-surface-2/60",
      )}
    >
      <button
        type="button"
        onClick={() => onSelect(id)}
        aria-current={selected === id ? "true" : undefined}
        className="flex min-w-0 flex-1 items-center gap-2 px-2.5 py-2 text-left"
      >
        <span className={cn("truncate text-[13px]", selected === id ? "font-medium text-fg" : "text-fg")}>{label}</span>
        {extra}
        <span className="ml-auto text-[11px] text-subtle tabular-nums">{count}</span>
      </button>
      {c && isAdmin && (
        <DropdownMenu
          aria-label={`Actions for ${c.name}`}
          items={[
            { label: "Edit", icon: <Pencil />, onSelect: () => onEdit(c) },
            { label: "Delete", icon: <Trash2 />, danger: true, onSelect: () => onDelete(c) },
          ]}
          trigger={
            <button
              type="button"
              aria-label={`Actions for ${c.name}`}
              className="mr-1 hidden size-6 items-center justify-center rounded-md text-muted group-hover:flex hover:bg-surface-3 hover:text-fg aria-expanded:flex"
            >
              <MoreHorizontal className="size-3.5" />
            </button>
          }
        />
      )}
    </div>
  );
  return (
    <nav aria-label="Collections" className="space-y-0.5">
      <div className="mb-2 flex items-center justify-between px-1">
        <p className="text-[11px] font-medium tracking-[0.06em] text-subtle uppercase">Collections</p>
        {isAdmin && (
          <IconButton label="New collection" icon={<Plus />} size="xs" onClick={onNew} />
        )}
      </div>
      {row("all", "All documents", totalDocs)}
      {collections.map((c) =>
        row(c.id, c.name, c.document_count, c.access === "restricted" ? <Lock className="size-3 shrink-0 text-subtle" /> : null, c),
      )}
    </nav>
  );
}

export default function KnowledgePage() {
  const qc = useQueryClient();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const { isAdmin, canWrite } = useRole();
  const selected = params.get("collection") ?? "all";
  const [query, setQuery] = useState("");
  const q = useDebounced(query.trim(), 300);
  const [status, setStatus] = useState("");
  const [uploads, setUploads] = useState<UploadRow[]>([]);
  const [targetChoice, setTargetChoice] = useState("");
  const [dialog, setDialog] = useState<{ open: boolean; collection: Collection | null }>({ open: false, collection: null });

  const collections = useCollections();
  const list = collections.data?.items ?? [];
  const docs = useDocuments({
    collection_id: selected === "all" ? undefined : selected,
    q: q || undefined,
    status: status || undefined,
  });
  const items = docs.data?.items ?? [];
  const selectedCollection = list.find((c) => c.id === selected) ?? null;
  const target = selected !== "all" ? selected : targetChoice || list[0]?.id || "";
  const totalDocs = list.reduce((s, c) => s + c.document_count, 0);

  const selectCollection = (id: string) => router.replace(id === "all" ? pathname : `${pathname}?collection=${id}`, { scroll: false });

  const deleteCollection = useMutation({
    mutationFn: (c: Collection) => api.delete(`/collections/${c.id}`),
    onSuccess: (_d, c) => {
      void qc.invalidateQueries({ queryKey: qk.collections });
      void qc.invalidateQueries({ queryKey: qk.documentLists });
      if (selected === c.id) selectCollection("all");
      toast.success("Collection deleted", { description: c.name });
    },
    meta: { errorTitle: "Couldn't delete the collection" },
  });

  const upload = (files: File[]) => {
    if (!target) {
      toast.warning("Create a collection first", { description: "Documents live inside collections." });
      return;
    }
    for (const file of files) {
      const row: UploadRow = { id: uid("up-"), name: file.name, size: file.size, progress: 0, status: "uploading", controller: new AbortController() };
      setUploads((u) => [...u, row]);
      const form = new FormData();
      form.append("file", file);
      form.append("collection_id", target);
      api
        .upload<KDocument>("/documents", form, {
          signal: row.controller.signal,
          onProgress: (p) => setUploads((u) => u.map((x) => (x.id === row.id ? { ...x, progress: p } : x))),
        })
        .then((doc) => {
          upsertDocument(qc, doc);
          void qc.invalidateQueries({ queryKey: qk.collections });
          setUploads((u) => u.filter((x) => x.id !== row.id));
        })
        .catch((err) => {
          if (err instanceof DOMException && err.name === "AbortError") {
            setUploads((u) => u.filter((x) => x.id !== row.id));
            return;
          }
          setUploads((u) => u.map((x) => (x.id === row.id ? { ...x, status: "error", error: errorMessage(err) } : x)));
        });
    }
  };

  const exportTraining = async () => {
    try {
      await api.download(
        "/knowledge/training-data",
        { collection_id: selected === "all" ? undefined : selected },
        `stair-training${selectedCollection ? `-${selectedCollection.name.toLowerCase().replace(/\W+/g, "-")}` : ""}.ndjson`,
      );
    } catch (err) {
      toast.error("Export failed", { description: errorMessage(err) });
    }
  };

  return (
    <PageContainer wide>
      <PageHeader
        title="Knowledge"
        description="Documents your agents can ground answers in. Ren builds a table of contents for each one and retrieves by structure (STAIR) as well as by meaning and keywords."
        actions={
          <>
            <Button icon={<Download className="size-4" />} onClick={() => void exportTraining()}>
              Export STAIR data
            </Button>
            <ButtonLink href="/knowledge/search" variant="primary" icon={<FlaskConical className="size-4" />}>
              Retrieval playground
            </ButtonLink>
          </>
        }
      />

      <StatsStrip />

      <div className="grid gap-6 lg:grid-cols-[240px_minmax(0,1fr)]">
        <aside>
          {collections.isLoading ? (
            <div className="space-y-2">
              {Array.from({ length: 5 }).map((_, i) => (
                <Skeleton key={i} className="h-8 w-full" />
              ))}
            </div>
          ) : collections.error ? (
            <ErrorState compact error={collections.error} onRetry={() => collections.refetch()} />
          ) : (
            <CollectionsNav
              collections={list}
              selected={selected}
              onSelect={selectCollection}
              isAdmin={isAdmin}
              totalDocs={totalDocs}
              onNew={() => setDialog({ open: true, collection: null })}
              onEdit={(c) => setDialog({ open: true, collection: c })}
              onDelete={async (c) => {
                const ok = await confirm({
                  title: `Delete “${c.name}”?`,
                  description: `Its ${c.document_count} documents will be removed from the knowledge base.`,
                  confirmLabel: "Delete collection",
                  danger: true,
                });
                if (ok) deleteCollection.mutate(c);
              }}
            />
          )}
        </aside>

        <div className="min-w-0 space-y-5">
          {selectedCollection?.description && <p className="-mt-1 text-[13px] text-muted">{selectedCollection.description}</p>}

          {canWrite &&
            (list.length === 0 && !collections.isLoading ? (
              <Card>
                <EmptyState
                  icon={<Layers />}
                  title="Start with a collection"
                  description={isAdmin ? "Collections group related documents — e.g. HR Policies, Product Docs, Contracts." : "Ask a workspace admin to create a collection, then upload documents into it."}
                  action={
                    isAdmin ? (
                      <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setDialog({ open: true, collection: null })}>
                        New collection
                      </Button>
                    ) : undefined
                  }
                />
              </Card>
            ) : (
              <FileDropzone
                onFiles={upload}
                accept={ACCEPT}
                title="Drop documents here, or click to browse"
                hint="PDF, DOCX, Markdown, TXT, HTML, XLSX, CSV, PPTX — multiple files at once"
              >
                {selected === "all" && list.length > 0 && (
                  <div className="mt-1 flex items-center gap-2 text-xs text-muted" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
                    <span>Upload into</span>
                    <Select selectSize="sm" value={target} onChange={(e) => setTargetChoice(e.target.value)} wrapperClassName="w-48" aria-label="Target collection">
                      {list.map((c) => (
                        <option key={c.id} value={c.id}>
                          {c.name}
                        </option>
                      ))}
                    </Select>
                  </div>
                )}
              </FileDropzone>
            ))}

          {uploads.length > 0 && (
            <Card className="divide-y divide-border">
              {uploads.map((u) => (
                <div key={u.id} className="flex items-center gap-3 px-4 py-2.5">
                  <FileText className={cn("size-4 shrink-0", u.status === "error" ? "text-danger" : "text-muted")} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-3 text-[12.5px]">
                      <span className="truncate font-medium text-fg">{u.name}</span>
                      <span className={cn("shrink-0 tabular-nums", u.status === "error" ? "text-danger" : "text-muted")}>
                        {u.status === "error" ? "Failed" : `${Math.round(u.progress * 100)}% of ${formatBytes(u.size)}`}
                      </span>
                    </div>
                    {u.status === "error" ? (
                      <p className="mt-0.5 truncate text-xs text-danger">{u.error}</p>
                    ) : (
                      <ProgressBar value={u.progress * 100} size="xs" className="mt-1.5" label={`Uploading ${u.name}`} />
                    )}
                  </div>
                  <IconButton
                    label={u.status === "error" ? "Dismiss" : "Cancel upload"}
                    icon={<X />}
                    size="xs"
                    onClick={() => {
                      u.controller.abort();
                      setUploads((rows) => rows.filter((x) => x.id !== u.id));
                    }}
                  />
                </div>
              ))}
            </Card>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <Input
              leftIcon={<Search />}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search documents"
              aria-label="Search documents"
              wrapperClassName="w-full sm:w-72"
            />
            <Select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Filter by status" wrapperClassName="w-full sm:w-44">
              <option value="">Any status</option>
              <option value="ready">Ready</option>
              <option value="failed">Failed</option>
              <option value="uploaded">Uploaded</option>
              <option value="parsing">Parsing</option>
              <option value="structuring">Structuring</option>
              <option value="embedding">Embedding</option>
              <option value="questions">Questions</option>
            </Select>
          </div>

          <Card className="overflow-hidden">
            {docs.isLoading ? (
              <SkeletonRows rows={6} />
            ) : docs.error && !docs.data ? (
              <ErrorState error={docs.error} onRetry={() => docs.refetch()} />
            ) : items.length === 0 ? (
              <EmptyState
                compact
                icon={<FileText />}
                title={q || status ? "No matching documents" : "No documents yet"}
                description={q || status ? "Try a different search or status." : "Upload policies, handbooks, contracts, reports — Ren will structure and index them."}
              />
            ) : (
              <TableContainer className={docs.isPlaceholderData ? "opacity-60" : undefined}>
                <Table>
                  <THead>
                    <tr>
                      <TH>Document</TH>
                      <TH>Status</TH>
                      <TH className="text-right">Sections / leaves</TH>
                      <TH className="text-right">Chunks</TH>
                      <TH>Structure</TH>
                      <TH>Uploaded</TH>
                      <TH className="w-10">
                        <span className="sr-only">Actions</span>
                      </TH>
                    </tr>
                  </THead>
                  <TBody>
                    {items.map((d) => (
                      <TR key={d.id} interactive onClick={() => router.push(`/knowledge/${d.id}`)}>
                        <TD className="max-w-[340px]">
                          <Link href={`/knowledge/${d.id}`} onClick={(e) => e.stopPropagation()} className="block truncate font-medium text-fg hover:underline">
                            {d.title}
                          </Link>
                          <span className="flex items-center gap-1.5 text-xs text-muted">
                            <span className="truncate">{d.filename}</span>
                            <span className="shrink-0 text-subtle">· {formatBytes(d.size)}</span>
                            {selected === "all" && (
                              <Badge tone="neutral" size="xs" className="ml-1">
                                {d.collection_name}
                              </Badge>
                            )}
                          </span>
                        </TD>
                        <TD>
                          <DocumentStatusCell doc={d} />
                        </TD>
                        <TD className="text-right text-muted tabular-nums">
                          {d.status === "ready" ? `${formatNumber(d.section_count)} / ${formatNumber(d.leaf_count)}` : "—"}
                        </TD>
                        <TD className="text-right text-muted tabular-nums">{d.status === "ready" ? formatNumber(d.chunk_count) : "—"}</TD>
                        <TD>
                          <StructureBadge source={d.structure_source} />
                        </TD>
                        <TD className="whitespace-nowrap text-muted">
                          <span title={formatDate(d.created_at)}>{timeAgo(d.created_at)}</span>
                          {d.uploaded_by_name && <span className="block text-xs text-subtle">{d.uploaded_by_name}</span>}
                        </TD>
                        <TD className="text-right" onClick={(e) => e.stopPropagation()}>
                          <DocumentActions doc={d} canWrite={canWrite} />
                        </TD>
                      </TR>
                    ))}
                  </TBody>
                </Table>
              </TableContainer>
            )}
          </Card>
        </div>
      </div>

      {dialog.open && (
        <CollectionDialog
          key={dialog.collection?.id ?? "new"}
          open={dialog.open}
          onOpenChange={(o) => setDialog((d) => ({ ...d, open: o }))}
          collection={dialog.collection}
          onSaved={(c) => !dialog.collection && selectCollection(c.id)}
        />
      )}
    </PageContainer>
  );
}
