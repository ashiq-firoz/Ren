"use client";

import { Eye } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { buttonClass, type ButtonSize, type ButtonVariant } from "@/components/ui/button";
import { viewerHref, type FileSource } from "@/lib/files";
import { formatBytes } from "@/lib/format";
import type { FileKind } from "@/lib/types";
import { cn } from "@/lib/utils";
import { FileKindIcon } from "./file-icon";
import { useFileClick } from "./file-viewer-context";

/**
 * Inline chip for a workspace path (e.g. in Ren's replies). A plain click opens the side viewer;
 * Ctrl/Cmd/Shift/middle-click keep the browser's behaviour (the href is the full-page viewer).
 */
export function FileLink({
  path,
  children,
  mono = true,
  className,
}: {
  /** Workspace-relative path, e.g. "output/report.md". */
  path: string;
  /** Label (defaults to the path). */
  children?: ReactNode;
  mono?: boolean;
  className?: string;
}) {
  const fileClick = useFileClick();
  return (
    <Link
      href={viewerHref({ path })}
      prefetch={false}
      onClick={fileClick({ path })}
      title={`Open ${path}`}
      className={cn(
        "rounded-[5px] border border-border bg-surface-2 px-1 py-px text-[0.86em] text-fg no-underline transition-colors [-webkit-box-decoration-break:clone] [box-decoration-break:clone]",
        "hover:border-border-strong hover:bg-surface-3 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus",
        className,
      )}
    >
      <FileKindIcon name={path} className="mr-1 inline-block size-[1.05em] align-[-0.17em] text-muted" />
      <span className={cn(mono && "font-mono")}>{children ?? path}</span>
    </Link>
  );
}

/** Compact file chip (icon, name, size) for lists of produced files; opens the viewer. */
export function FileChip({
  source,
  name,
  kind,
  size,
  className,
}: {
  source: FileSource;
  name: string;
  kind?: FileKind | null;
  size?: number | null;
  className?: string;
}) {
  const fileClick = useFileClick();
  return (
    <Link
      href={viewerHref(source)}
      prefetch={false}
      onClick={fileClick(source)}
      title={`Open ${name}`}
      className={cn(
        "inline-flex h-8 max-w-[280px] items-center gap-2 rounded-lg border border-border bg-surface px-2.5 text-xs transition-colors hover:border-border-strong hover:bg-surface-2",
        className,
      )}
    >
      <FileKindIcon kind={kind} name={name} className="size-3.5 text-muted" />
      <span className="truncate font-medium text-fg">{name}</span>
      {size !== null && size !== undefined && <span className="shrink-0 text-subtle tabular-nums">{formatBytes(size)}</span>}
    </Link>
  );
}

/** "View" button (a link to the full-page viewer that opens the side viewer on a plain click). */
export function ViewFileButton({
  source,
  name,
  size = "xs",
  variant = "secondary",
  iconOnly = false,
  className,
}: {
  source: FileSource;
  /** For the accessible label: "View report.md". */
  name?: string | null;
  size?: ButtonSize;
  variant?: ButtonVariant;
  iconOnly?: boolean;
  className?: string;
}) {
  const fileClick = useFileClick();
  const label = name ? `View ${name}` : "View";
  return (
    <Link
      href={viewerHref(source)}
      prefetch={false}
      onClick={fileClick(source)}
      aria-label={iconOnly || name ? label : undefined}
      title={iconOnly ? label : undefined}
      className={buttonClass(variant, size, cn(iconOnly && (size === "xs" ? "w-7 px-0" : "w-8 px-0"), className))}
    >
      <Eye className={size === "xs" ? "size-3" : "size-3.5"} />
      {!iconOnly && "View"}
    </Link>
  );
}
