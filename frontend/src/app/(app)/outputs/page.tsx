"use client";

import { ChevronRight, Download, FileBox, Folder, FolderOpen, HardDrive, MessageSquare, Search, Upload } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Fragment, Suspense, useEffect, useState, type MouseEvent, type ReactNode } from "react";
import { FileKindIcon } from "@/components/files/file-icon";
import { ViewFileButton } from "@/components/files/file-link";
import { useFileClick, useOptionalFileViewer } from "@/components/files/file-viewer-context";
import { PageContainer, PageHeader } from "@/components/shell/page-header";
import { Button, IconButton } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { CopyButton } from "@/components/ui/copy-button";
import { EmptyState, ErrorState, Skeleton, SkeletonRows } from "@/components/ui/feedback";
import { Input, Select } from "@/components/ui/input";
import { Tabs } from "@/components/ui/tabs";
import { useArtifacts, useWorkspaceFiles } from "@/hooks/queries";
import { useDebounced } from "@/hooks/use-browser";
import { downloadFile, KIND_FILTERS, TOP_FOLDERS, viewerHref, type FileSource } from "@/lib/files";
import { formatBytes, formatDateTime, timeAgo } from "@/lib/format";
import type { Artifact, FileKind, WorkspaceFile } from "@/lib/types";
import { cn } from "@/lib/utils";

type Tab = "generated" | "uploads" | "files";
const PAGE_SIZE = 50;

/** Open in the side viewer (or the full-page viewer outside the provider). */
function useOpenFile() {
  const viewer = useOptionalFileViewer();
  const router = useRouter();
  return (source: FileSource) => (viewer ? viewer.openFile(source) : router.push(viewerHref(source)));
}

/** Row click that leaves nested links and buttons alone (they handle themselves, incl. Ctrl/Cmd-click). */
function rowClick(fn: () => void) {
  return (e: MouseEvent<HTMLElement>) => {
    if ((e.target as HTMLElement).closest("a, button")) return;
    fn();
  };
}

function RowActions({ source, name, path }: { source: FileSource; name: string; path: string }) {
  return (
    <div className="flex shrink-0 items-center gap-0.5" onClick={(e) => e.stopPropagation()}>
      <ViewFileButton source={source} name={name} iconOnly variant="ghost" />
      <IconButton label={`Download ${name}`} icon={<Download />} onClick={() => void downloadFile(source, name)} />
      <CopyButton text={path} label="Copy path" />
    </div>
  );
}

function Dot() {
  return (
    <span aria-hidden className="text-subtle">
      ·
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* Generated / uploaded artifacts                                      */
/* ------------------------------------------------------------------ */

/** Desktop columns: file · size · created · produced by · actions. */
const ARTIFACT_GRID = "md:grid-cols-[minmax(0,1fr)_80px_96px_minmax(0,220px)_92px]";

function Provenance({ artifact: a }: { artifact: Artifact }) {
  if (a.source === "uploaded" || (!a.agent_name && !a.task_id)) {
    return a.conversation_id ? (
      <Link
        href={`/chat/${a.conversation_id}`}
        onClick={(e) => e.stopPropagation()}
        className="inline-flex min-w-0 items-center gap-1 hover:text-fg hover:underline"
      >
        <MessageSquare className="size-3 shrink-0" /> <span className="truncate">Uploaded in chat</span>
      </Link>
    ) : (
      <span>Uploaded</span>
    );
  }
  return (
    <span className="flex min-w-0 flex-col">
      {a.agent_name && (
        <span className="truncate">
          by <span className="text-fg">{a.agent_name}</span>
        </span>
      )}
      {a.task_id && (
        <Link
          href={`/tasks/${a.task_id}`}
          onClick={(e) => e.stopPropagation()}
          title={a.task_title ?? undefined}
          className="truncate hover:text-fg hover:underline"
        >
          {a.task_title || "View task"}
        </Link>
      )}
    </span>
  );
}

function ArtifactRow({ artifact: a, onOpen }: { artifact: Artifact; onOpen: (source: FileSource) => void }) {
  const fileClick = useFileClick();
  const source: FileSource = { artifactId: a.id };
  return (
    <li
      onClick={rowClick(() => onOpen(source))}
      className={cn("grid cursor-pointer grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 px-4 py-3 transition-colors hover:bg-surface-2/60", ARTIFACT_GRID)}
    >
      <div className="flex min-w-0 items-start gap-3">
        <span className="mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-lg border border-border bg-surface-2 text-muted">
          <FileKindIcon kind={a.kind} name={a.name} className="size-[18px]" />
        </span>
        <div className="min-w-0">
          <Link
            href={viewerHref(source)}
            prefetch={false}
            onClick={fileClick(source)}
            className="block truncate text-[13.5px] font-medium text-fg hover:underline"
          >
            {a.name}
          </Link>
          <p className="truncate font-mono text-[11.5px] text-subtle" title={a.path}>
            {a.path}
          </p>
          {/* Phones: meta on one line under the name. */}
          <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-muted md:hidden">
            <span className="tabular-nums">{formatBytes(a.size)}</span>
            <Dot />
            <span title={formatDateTime(a.created_at)}>{timeAgo(a.created_at)}</span>
            {(a.agent_name || a.task_id) && <Dot />}
            {a.agent_name && (
              <span>
                by <span className="text-fg">{a.agent_name}</span>
              </span>
            )}
          </div>
        </div>
      </div>
      <span className="hidden text-right text-[13px] text-muted tabular-nums md:block">{formatBytes(a.size)}</span>
      <span className="hidden text-[13px] text-muted md:block" title={formatDateTime(a.created_at)}>
        {timeAgo(a.created_at)}
      </span>
      <div className="hidden min-w-0 text-xs text-muted md:block">
        <Provenance artifact={a} />
      </div>
      <div className="flex justify-end">
        <RowActions source={source} name={a.name} path={a.path} />
      </div>
    </li>
  );
}

function ArtifactList({
  tab,
  q,
  kind,
  onClearFilters,
}: {
  tab: "generated" | "uploads";
  q: string;
  kind: FileKind | "";
  onClearFilters: () => void;
}) {
  const open = useOpenFile();
  const filterKey = `${tab}|${q}|${kind}`;
  // "Load more" grows the page; a new filter starts over (keyed state, no effect).
  const [paging, setPaging] = useState({ key: filterKey, pages: 1 });
  const pages = paging.key === filterKey ? paging.pages : 1;
  const list = useArtifacts({
    q: q || undefined,
    kind: kind || undefined,
    source: tab === "uploads" ? "uploaded" : "generated",
    limit: PAGE_SIZE * pages,
    offset: 0,
  });
  const items = list.data?.items ?? [];
  const total = list.data?.total ?? 0;
  const loadingMore = list.isFetching && list.isPlaceholderData && pages > 1 && paging.key === filterKey;
  const filtered = !!q || !!kind;

  if (list.isLoading)
    return (
      <Card>
        <SkeletonRows rows={8} />
      </Card>
    );
  if (list.error && !list.data)
    return (
      <Card>
        <ErrorState error={list.error} onRetry={() => void list.refetch()} />
      </Card>
    );
  if (items.length === 0)
    return (
      <Card>
        <EmptyState
          icon={tab === "uploads" ? <Upload /> : <FileBox />}
          title={filtered ? "No matching files" : tab === "uploads" ? "No uploads yet" : "No files yet"}
          description={
            filtered
              ? "Try a different search or file type."
              : tab === "uploads"
                ? "Files you attach in chat are kept here."
                : "When agents write reports, spreadsheets, decks or charts, they show up here."
          }
          action={
            filtered ? (
              <Button size="sm" onClick={onClearFilters}>
                Clear filters
              </Button>
            ) : undefined
          }
        />
      </Card>
    );

  return (
    <Card className="overflow-hidden">
      <div
        aria-hidden
        className={cn(
          "hidden gap-x-4 border-b border-border px-4 py-2.5 text-[11px] font-medium tracking-wide text-subtle uppercase md:grid",
          ARTIFACT_GRID,
        )}
      >
        <span>File</span>
        <span className="text-right">Size</span>
        <span>Created</span>
        <span>{tab === "uploads" ? "Source" : "Produced by"}</span>
        <span />
      </div>
      <ul
        className={cn(
          "divide-y divide-border",
          list.isFetching && list.isPlaceholderData && !loadingMore && "opacity-60 transition-opacity",
        )}
      >
        {items.map((a) => (
          <ArtifactRow key={a.id} artifact={a} onOpen={open} />
        ))}
      </ul>
      {(total > items.length || pages > 1) && (
        <div className="flex items-center justify-between gap-3 border-t border-border px-4 py-2.5 text-xs text-muted">
          <span className="tabular-nums">
            Showing {items.length.toLocaleString()} of {total.toLocaleString()}
          </span>
          {total > items.length && (
            <Button size="xs" variant="secondary" loading={loadingMore} onClick={() => setPaging({ key: filterKey, pages: pages + 1 })}>
              Load more
            </Button>
          )}
        </div>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/* Workspace browser                                                   */
/* ------------------------------------------------------------------ */

const FOLDER_ORDER = TOP_FOLDERS.map((f) => f.name);

function sortEntries(items: WorkspaceFile[], atRoot: boolean): WorkspaceFile[] {
  if (!atRoot) return items.slice().sort((a, b) => Number(b.is_dir) - Number(a.is_dir) || a.name.localeCompare(b.name));
  const rank = (name: string) => {
    const i = FOLDER_ORDER.indexOf(name);
    return i < 0 ? FOLDER_ORDER.length : i;
  };
  return items.slice().sort((a, b) => rank(a.name) - rank(b.name) || a.name.localeCompare(b.name));
}

function Breadcrumb({ path, onNavigate }: { path: string; onNavigate: (path: string) => void }) {
  const parts = path.split("/").filter(Boolean);
  const crumb = "inline-flex max-w-[200px] items-center gap-1.5 truncate rounded-md px-1.5 py-1 transition-colors hover:bg-surface-2 hover:text-fg";
  return (
    <nav aria-label="Folder" className="flex min-w-0 flex-wrap items-center gap-0.5 text-[13px] text-muted">
      <button type="button" onClick={() => onNavigate("")} className={cn(crumb, parts.length === 0 && "font-medium text-fg")}>
        <HardDrive className="size-3.5 shrink-0" /> All files
      </button>
      {parts.map((part, i) => {
        const last = i === parts.length - 1;
        return (
          <Fragment key={i}>
            <ChevronRight aria-hidden className="size-3.5 shrink-0 text-subtle" />
            <button
              type="button"
              onClick={() => onNavigate(parts.slice(0, i + 1).join("/"))}
              aria-current={last ? "location" : undefined}
              className={cn(crumb, last && "font-medium text-fg")}
            >
              {part}
            </button>
          </Fragment>
        );
      })}
    </nav>
  );
}

function FileBrowser({ path, onNavigate }: { path: string; onNavigate: (path: string) => void }) {
  const open = useOpenFile();
  const fileClick = useFileClick();
  const { data, isLoading, error, refetch } = useWorkspaceFiles(path);
  const atRoot = path === "";
  const items = sortEntries(data?.items ?? [], atRoot);

  let body: ReactNode;
  if (isLoading) body = <SkeletonRows rows={6} />;
  else if (error && !data) body = <ErrorState error={error} title="Couldn't open this folder" onRetry={() => void refetch()} />;
  else if (items.length === 0)
    body = <EmptyState compact icon={<FolderOpen />} title="This folder is empty" description="Files agents write here will show up as they're created." />;
  else
    body = (
      <ul className="divide-y divide-border">
        {items.map((entry) => {
          const top = atRoot ? TOP_FOLDERS.find((f) => f.name === entry.name) : undefined;
          if (entry.is_dir) {
            return (
              <li key={entry.path}>
                <button
                  type="button"
                  onClick={() => onNavigate(entry.path)}
                  className="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-surface-2/60"
                >
                  <span className="flex size-9 shrink-0 items-center justify-center rounded-lg border border-border bg-surface-2 text-muted">
                    <Folder className="size-[18px]" aria-hidden />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13.5px] font-medium text-fg">{entry.name}</span>
                    {top && <span className="block truncate text-xs text-muted">{top.description}</span>}
                  </span>
                  {!atRoot && entry.modified_at && (
                    <span className="hidden shrink-0 text-xs text-muted sm:block" title={formatDateTime(entry.modified_at)}>
                      {timeAgo(entry.modified_at)}
                    </span>
                  )}
                  <ChevronRight className="size-4 shrink-0 text-subtle" aria-hidden />
                </button>
              </li>
            );
          }
          const source: FileSource = { path: entry.path };
          return (
            <li
              key={entry.path}
              onClick={rowClick(() => open(source))}
              className="flex cursor-pointer items-center gap-3 px-4 py-3 transition-colors hover:bg-surface-2/60"
            >
              <span className="flex size-9 shrink-0 items-center justify-center rounded-lg border border-border bg-surface-2 text-muted">
                <FileKindIcon name={entry.name} className="size-[18px]" />
              </span>
              <span className="min-w-0 flex-1">
                <Link
                  href={viewerHref(source)}
                  prefetch={false}
                  onClick={fileClick(source)}
                  className="block truncate text-[13.5px] font-medium text-fg hover:underline"
                >
                  {entry.name}
                </Link>
                <span className="block truncate text-xs text-muted">
                  <span className="tabular-nums">{formatBytes(entry.size)}</span>
                  {entry.modified_at && (
                    <>
                      {" · "}
                      <span title={formatDateTime(entry.modified_at)}>Modified {timeAgo(entry.modified_at)}</span>
                    </>
                  )}
                </span>
              </span>
              <RowActions source={source} name={entry.name} path={entry.path} />
            </li>
          );
        })}
      </ul>
    );

  return (
    <Card className="overflow-hidden">
      <div className="border-b border-border px-3 py-2">
        <Breadcrumb path={path} onNavigate={onNavigate} />
      </div>
      {body}
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/* Page                                                                */
/* ------------------------------------------------------------------ */

function OutputsView() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();

  const tabParam = params.get("tab");
  const tab: Tab = tabParam === "uploads" || tabParam === "files" ? tabParam : "generated";
  const kindParam = params.get("kind") ?? "";
  const kind: FileKind | "" = KIND_FILTERS.some((k) => k.value === kindParam) ? (kindParam as FileKind) : "";
  const folder = (params.get("path") ?? "").replace(/^\/+|\/+$/g, "");
  const urlQ = params.get("q") ?? "";
  const [query, setQuery] = useState(urlQ);
  const q = useDebounced(query.trim(), 300);

  const replaceParams = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params.toString());
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === "" || (k === "tab" && v === "generated")) next.delete(k);
      else next.set(k, v);
    }
    const s = next.toString();
    router.replace(`${pathname}${s ? `?${s}` : ""}`, { scroll: false });
  };

  // Keep the (debounced) search in the URL so the view can be shared or restored.
  useEffect(() => {
    if (q === urlQ) return;
    const next = new URLSearchParams(params.toString());
    if (q) next.set("q", q);
    else next.delete("q");
    const s = next.toString();
    router.replace(`${pathname}${s ? `?${s}` : ""}`, { scroll: false });
  }, [q, urlQ, params, pathname, router]);

  const clearFilters = () => {
    setQuery(""); // the debounced search then drops `q` from the URL
    replaceParams({ kind: null });
  };

  return (
    <PageContainer>
      <PageHeader title="Outputs" description="Everything your agents produced. Click a file to view it." />

      <div className="mb-4 space-y-3">
        <Tabs<Tab>
          aria-label="Outputs"
          idPrefix="outputs-tab"
          value={tab}
          onValueChange={(v) => replaceParams({ tab: v })}
          items={[
            { value: "generated", label: "Generated" },
            { value: "uploads", label: "Uploads" },
            { value: "files", label: "All files" },
          ]}
        />
        {tab !== "files" && (
          <div className="flex flex-wrap items-center gap-2">
            <Input
              leftIcon={<Search />}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search by name or path"
              aria-label="Search files"
              wrapperClassName="w-full sm:w-72"
            />
            <Select
              value={kind}
              onChange={(e) => replaceParams({ kind: e.target.value })}
              aria-label="Filter by file type"
              wrapperClassName="w-full sm:w-52"
            >
              <option value="">All file types</option>
              {KIND_FILTERS.map((k) => (
                <option key={k.value} value={k.value}>
                  {k.label}
                </option>
              ))}
            </Select>
          </div>
        )}
      </div>

      <div role="tabpanel" aria-labelledby={`outputs-tab-${tab}`}>
        {tab === "files" ? (
          <FileBrowser path={folder} onNavigate={(p) => replaceParams({ path: p || null })} />
        ) : (
          <ArtifactList tab={tab} q={q} kind={kind} onClearFilters={clearFilters} />
        )}
      </div>
    </PageContainer>
  );
}

function OutputsFallback() {
  return (
    <PageContainer>
      <Skeleton className="h-9 w-40" />
      <Skeleton className="mt-3 h-4 w-72 max-w-full" />
      <Skeleton className="mt-8 h-64 w-full rounded-card" />
    </PageContainer>
  );
}

export default function OutputsPage() {
  // useSearchParams (tab / search / type / folder) needs a Suspense boundary for prerendering.
  return (
    <Suspense fallback={<OutputsFallback />}>
      <OutputsView />
    </Suspense>
  );
}
