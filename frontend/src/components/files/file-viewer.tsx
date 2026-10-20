"use client";

import { Code2, Download, Eye, Info, Maximize2, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
import { Button, IconButton } from "@/components/ui/button";
import { SegmentedControl } from "@/components/ui/controls";
import { CopyButton } from "@/components/ui/copy-button";
import { EmptyState, ErrorState, Notice, Skeleton, SkeletonText } from "@/components/ui/feedback";
import { Markdown } from "@/components/ui/markdown";
import { useFilePreview } from "@/hooks/queries";
import { api, ApiError, errorMessage, rawFetch } from "@/lib/api";
import { FILE_KIND_LABEL, downloadFile, fileKindFromName, fileNameOf, type FileSource } from "@/lib/files";
import { formatBytes, formatDateTime, timeAgo } from "@/lib/format";
import { toast } from "@/lib/toast";
import type { FilePreview } from "@/lib/types";
import { cn } from "@/lib/utils";
import { FileKindIcon } from "./file-icon";

type Layout = "drawer" | "page";

/* ------------------------------------------------------------------ */
/* Raw bytes (images, PDFs, media) → object URL                        */
/* ------------------------------------------------------------------ */

/** Fetch `download` with the bearer header and expose it as an object URL (revoked on unmount / change). */
function useObjectUrl(download: FilePreview["download"] | null, type: string | null) {
  const path = download?.path ?? null;
  const qs = new URLSearchParams(download?.query ?? {}).toString();
  const [attempt, setAttempt] = useState(0);
  const key = path ? `${path}?${qs}|${type ?? ""}|${attempt}` : null;
  const [state, setState] = useState<{ key: string; url: string | null; error: unknown } | null>(null);

  useEffect(() => {
    if (!path) return;
    const k = `${path}?${qs}|${type ?? ""}|${attempt}`;
    const ctrl = new AbortController();
    let url: string | null = null;
    rawFetch("GET", path, { query: Object.fromEntries(new URLSearchParams(qs)), signal: ctrl.signal })
      .then((res) => res.blob())
      .then((blob) => {
        if (ctrl.signal.aborted) return;
        // Force the declared type so the browser renders it as that (never sniffed as HTML).
        url = URL.createObjectURL(type && blob.type !== type ? new Blob([blob], { type }) : blob);
        setState({ key: k, url, error: null });
      })
      .catch((error: unknown) => {
        if (!ctrl.signal.aborted) setState({ key: k, url: null, error });
      });
    return () => {
      ctrl.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [path, qs, type, attempt]);

  const current = state && state.key === key ? state : null;
  return {
    url: current?.url ?? null,
    error: current?.error ?? null,
    loading: !!key && !current,
    retry: () => setAttempt((n) => n + 1),
  };
}

function mediaType(preview: FilePreview): string | null {
  if (preview.render === "pdf") return "application/pdf";
  const prefix = preview.render === "image" ? "image/" : preview.render === "audio" ? "audio/" : preview.render === "video" ? "video/" : null;
  return prefix && preview.mime?.startsWith(prefix) ? preview.mime : null;
}

/* ------------------------------------------------------------------ */
/* Bodies                                                              */
/* ------------------------------------------------------------------ */

function TruncatedNotice() {
  return (
    <Notice tone="info" icon={<Info />}>
      Showing the beginning of this file — download it for the full content.
    </Notice>
  );
}

export function CodeBlock({ content, language, className }: { content: string; language?: string | null; className?: string }) {
  return (
    <div className={cn("overflow-hidden rounded-card border border-border bg-[var(--code-bg)]", className)}>
      <div className="flex h-9 items-center justify-between border-b border-border px-3 text-[11px] text-subtle">
        <span className="font-mono">{language || "text"}</span>
        <CopyButton text={content} size="xs" label="Copy" showLabel />
      </div>
      <pre className="overflow-x-auto p-4 font-mono text-[12.5px] leading-relaxed text-fg">
        <code>{content}</code>
      </pre>
    </div>
  );
}

function TableBody({ table, layout }: { table: NonNullable<FilePreview["table"]>; layout: Layout }) {
  const shown = table.rows.length;
  return (
    <div className="space-y-2">
      {table.total_rows > shown && (
        <p className="text-xs text-muted">
          Showing the first {shown.toLocaleString()} of {table.total_rows.toLocaleString()} rows — download for the full file.
        </p>
      )}
      <div
        className={cn(
          "overflow-auto rounded-card border border-border bg-surface",
          layout === "drawer" ? "max-h-[calc(100dvh-260px)]" : "max-h-[75vh]",
        )}
      >
        <table className="w-full border-collapse text-[12.5px]">
          <thead>
            <tr>
              <th className="sticky top-0 z-[1] w-10 border-b border-border bg-surface-2 px-2.5 py-2 text-right font-medium text-subtle">
                <span className="sr-only">Row</span>#
              </th>
              {table.columns.map((c, i) => (
                <th
                  key={i}
                  scope="col"
                  className="sticky top-0 z-[1] border-b border-border bg-surface-2 px-3 py-2 text-left font-semibold whitespace-nowrap text-fg"
                >
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {table.rows.map((row, r) => (
              <tr key={r} className="hover:bg-surface-2/50">
                <td className="px-2.5 py-1.5 text-right text-[11px] text-subtle tabular-nums">{r + 1}</td>
                {table.columns.map((_, c) => (
                  <td key={c} title={row[c] ?? ""} className="max-w-[360px] truncate px-3 py-1.5 whitespace-nowrap text-fg">
                    {row[c] ?? ""}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {shown === 0 && <p className="px-4 py-6 text-center text-[13px] text-muted">This table has no rows.</p>}
      </div>
    </div>
  );
}

function HtmlBody({ preview, layout }: { preview: FilePreview; layout: Layout }) {
  const [view, setView] = useState<"preview" | "source">("preview");
  const content = preview.content ?? "";
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted">Shown in a sandbox — scripts and forms are disabled.</p>
        <SegmentedControl
          size="sm"
          aria-label="HTML view"
          value={view}
          onChange={setView}
          options={[
            { value: "preview", label: "Preview", icon: <Eye /> },
            { value: "source", label: "Source", icon: <Code2 /> },
          ]}
        />
      </div>
      {view === "preview" ? (
        <iframe
          title={preview.name}
          sandbox=""
          referrerPolicy="no-referrer"
          srcDoc={content}
          // Web pages expect a light canvas; ivory keeps them readable in the dark theme too.
          style={{ colorScheme: "light" }}
          className={cn(
            "block w-full rounded-card border border-border bg-f69-ivory",
            layout === "drawer" ? "h-[calc(100dvh-240px)] min-h-[420px]" : "h-[75vh] min-h-[480px]",
          )}
        />
      ) : (
        <CodeBlock content={content} language="html" />
      )}
    </div>
  );
}

function MediaBody({ preview, layout }: { preview: FilePreview; layout: Layout }) {
  const { url, error, loading, retry } = useObjectUrl(preview.download, mediaType(preview));
  if (error) return <ErrorState compact error={error} title="Couldn't load the file" onRetry={retry} />;
  if (loading || !url) return <Skeleton className={cn("w-full rounded-card", layout === "drawer" ? "h-[60vh]" : "h-[70vh]")} />;
  switch (preview.render) {
    case "image":
      return (
        <div className="flex justify-center rounded-card border border-border bg-surface-2 p-4">
          {/* eslint-disable-next-line @next/next/no-img-element -- authenticated blob URL, not a static asset */}
          <img src={url} alt={preview.name} className="max-h-[75vh] max-w-full rounded-md object-contain" />
        </div>
      );
    case "pdf":
      return (
        <iframe
          title={preview.name}
          src={url}
          className={cn("block w-full rounded-card border border-border bg-surface-2", layout === "drawer" ? "h-[calc(100dvh-200px)]" : "h-[80vh]")}
        />
      );
    case "audio":
      return (
        <div className="rounded-card border border-border bg-surface-2 p-5">
          <audio controls src={url} className="w-full" aria-label={preview.name} />
        </div>
      );
    case "video":
      return <video controls src={url} className="max-h-[75vh] w-full rounded-card border border-border bg-surface-2" aria-label={preview.name} />;
    default:
      return null;
  }
}

function NoPreview({ preview, onDownload, downloading }: { preview: FilePreview; onDownload: () => void; downloading: boolean }) {
  return (
    <EmptyState
      icon={<FileKindIcon kind={preview.kind} name={preview.name} />}
      title="Preview not available for this file type"
      description={`${FILE_KIND_LABEL[preview.kind] ?? "File"} · ${formatBytes(preview.size)}. Download it to open it in its own app.`}
      action={
        <Button variant="primary" size="sm" icon={<Download className="size-3.5" />} loading={downloading} onClick={onDownload}>
          Download
        </Button>
      }
    />
  );
}

function PreviewBody({
  preview,
  layout,
  onDownload,
  downloading,
}: {
  preview: FilePreview;
  layout: Layout;
  onDownload: () => void;
  downloading: boolean;
}) {
  const pad = layout === "drawer" ? "px-5 py-5" : "p-5 sm:p-6";
  const hasText = typeof preview.content === "string";
  let body: ReactNode;
  switch (preview.render) {
    case "markdown":
      body = hasText ? (
        <div className={cn("mx-auto w-full max-w-[760px] space-y-4", layout === "drawer" ? "px-5 py-6 sm:px-8" : "px-5 py-6 sm:px-10 sm:py-8")}>
          {preview.converted_from && (
            <p className="flex items-center gap-1.5 text-xs text-subtle">
              <Info className="size-3.5" /> Converted from {preview.converted_from.toUpperCase()} for preview — download for the original
              formatting.
            </p>
          )}
          {preview.truncated && <TruncatedNotice />}
          <Markdown content={preview.content ?? ""} />
        </div>
      ) : null;
      break;
    case "code":
      body = hasText ? (
        <div className={cn("space-y-3", pad)}>
          {preview.truncated && <TruncatedNotice />}
          <CodeBlock content={preview.content ?? ""} language={preview.language} />
        </div>
      ) : null;
      break;
    case "table":
      body = preview.table ? (
        <div className={cn("space-y-3", pad)}>
          {preview.truncated && preview.table.total_rows <= preview.table.rows.length && <TruncatedNotice />}
          <TableBody table={preview.table} layout={layout} />
        </div>
      ) : null;
      break;
    case "html":
      body = hasText ? (
        <div className={cn("space-y-3", pad)}>
          {preview.truncated && <TruncatedNotice />}
          <HtmlBody preview={preview} layout={layout} />
        </div>
      ) : null;
      break;
    case "image":
    case "pdf":
    case "audio":
    case "video":
      body = (
        <div className={pad}>
          <MediaBody preview={preview} layout={layout} />
        </div>
      );
      break;
    default:
      body = null;
  }
  return body ?? <NoPreview preview={preview} onDownload={onDownload} downloading={downloading} />;
}

/* ------------------------------------------------------------------ */
/* Viewer                                                              */
/* ------------------------------------------------------------------ */

function ViewerSkeleton({ layout }: { layout: Layout }) {
  return (
    <div className={layout === "drawer" ? "px-5 py-6" : "p-6"} aria-busy>
      <Skeleton className="h-5 w-2/3" />
      <SkeletonText lines={8} className="mt-6" />
    </div>
  );
}

export interface FileViewerProps {
  source: FileSource;
  /** drawer = fills a side sheet (own scroll area); page = header + body card for the full-page route. */
  layout?: Layout;
  onClose?: () => void;
  onOpenFullPage?: () => void;
  /** Called before following a link out of the viewer (e.g. to the task), so a drawer can close. */
  onNavigate?: () => void;
  className?: string;
}

/** Header (name, path, size, dates, agent + task) and a body chosen by `preview.render`. */
export function FileViewer({ source, layout = "page", onClose, onOpenFullPage, onNavigate, className }: FileViewerProps) {
  const { data: preview, isLoading, error, refetch } = useFilePreview(source);
  const [downloading, setDownloading] = useState(false);

  const sourcePath = "path" in source ? source.path : null;
  const name = preview?.name ?? (sourcePath ? fileNameOf(sourcePath) : "File");
  const path = preview?.path ?? sourcePath;
  const kind = preview?.kind ?? fileKindFromName(name);
  const notFound = error instanceof ApiError && error.isNotFound;
  const drawer = layout === "drawer";

  const download = async () => {
    setDownloading(true);
    try {
      if (preview?.download?.path) await api.download(preview.download.path, preview.download.query, preview.name);
      else await downloadFile(source, name);
    } catch (err) {
      toast.error("Download failed", { description: errorMessage(err) });
    } finally {
      setDownloading(false);
    }
  };

  const created = preview?.created_at ?? null;
  const modified = preview?.modified_at ?? null;
  const meta: ReactNode[] = [];
  if (preview) {
    meta.push(<span key="kind">{FILE_KIND_LABEL[kind] ?? "File"}</span>);
    meta.push(<span key="size" className="tabular-nums">{formatBytes(preview.size)}</span>);
    if (created || modified) {
      meta.push(
        <span key="time" title={formatDateTime(created ?? modified)}>
          {created ? "Created" : "Modified"} {timeAgo(created ?? modified)}
        </span>,
      );
    }
    if (preview.agent_name) {
      meta.push(
        <span key="agent">
          by <span className="text-fg">{preview.agent_name}</span>
        </span>,
      );
    }
    if (preview.task_id) {
      meta.push(
        <Link key="task" href={`/tasks/${preview.task_id}`} onClick={onNavigate} className="max-w-[260px] truncate text-fg underline-offset-2 hover:underline">
          {preview.task_title || "View task"}
        </Link>,
      );
    }
  }

  const TitleTag = drawer ? "h2" : "h1";
  const header = (
    <div className={cn("flex items-start gap-3", drawer ? "shrink-0 border-b border-border px-5 py-4" : "mb-5")}>
      <span
        className={cn(
          "flex shrink-0 items-center justify-center rounded-[10px] border border-border bg-surface-2 text-muted",
          drawer ? "size-10 [&_svg]:size-5" : "size-11 [&_svg]:size-5",
        )}
      >
        <FileKindIcon kind={kind} name={name} />
      </span>
      <div className="min-w-0 flex-1">
        <TitleTag
          className={cn("font-serif-display leading-tight text-fg", drawer ? "truncate text-[22px]" : "text-[28px] break-words sm:text-[30px]")}
        >
          {name}
        </TitleTag>
        {path && <p className="mt-0.5 truncate font-mono text-xs text-subtle" title={path}>{path}</p>}
        {meta.length > 0 && (
          <div className="mt-1.5 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-muted">
            {meta.map((m, i) => (
              <span key={i} className="inline-flex min-w-0 items-center gap-1.5">
                {i > 0 && <span aria-hidden className="text-subtle">·</span>}
                {m}
              </span>
            ))}
          </div>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <Button
          size="sm"
          variant="secondary"
          icon={<Download className="size-3.5" />}
          loading={downloading}
          onClick={() => void download()}
          aria-label="Download"
          disabled={notFound}
        >
          <span className="max-sm:hidden">Download</span>
        </Button>
        {path && <CopyButton text={path} label="Copy path" />}
        {onOpenFullPage && <IconButton label="Open full page" icon={<Maximize2 />} onClick={onOpenFullPage} />}
        {onClose && <IconButton label="Close" icon={<X />} onClick={onClose} />}
      </div>
    </div>
  );

  let body: ReactNode;
  if (isLoading) body = <ViewerSkeleton layout={layout} />;
  else if (error || !preview)
    body = notFound ? (
      <ErrorState error={error} title="This file no longer exists" />
    ) : (
      <ErrorState error={error} title="Couldn't open this file" onRetry={() => void refetch()} />
    );
  else body = <PreviewBody preview={preview} layout={layout} onDownload={() => void download()} downloading={downloading} />;

  if (drawer) {
    return (
      <div className={cn("flex h-full min-h-0 flex-col", className)}>
        {header}
        <div className="min-h-0 flex-1 overflow-y-auto">{body}</div>
      </div>
    );
  }
  return (
    <div className={className}>
      {header}
      <div className="overflow-hidden rounded-card border border-border bg-surface">{body}</div>
    </div>
  );
}

