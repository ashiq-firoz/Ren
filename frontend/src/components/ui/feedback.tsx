"use client";

import { AlertTriangle, RefreshCw, WifiOff } from "lucide-react";
import type { ReactNode } from "react";
import { ApiError, errorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Button } from "./button";

export function Skeleton({ className, style }: { className?: string; style?: React.CSSProperties }) {
  return <div aria-hidden className={cn("skeleton h-4", className)} style={style} />;
}

export function SkeletonText({ lines = 3, className }: { lines?: number; className?: string }) {
  return (
    <div className={cn("space-y-2", className)} aria-hidden>
      {Array.from({ length: lines }).map((_, i) => (
        <Skeleton key={i} className="h-3.5" style={{ width: `${i === lines - 1 ? 60 : 92 - i * 6}%` }} />
      ))}
    </div>
  );
}

export function SkeletonRows({ rows = 5, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("divide-y divide-border", className)} aria-hidden>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex items-center gap-3 px-4 py-3">
          <Skeleton className="size-7 rounded-md" />
          <div className="flex-1 space-y-1.5">
            <Skeleton className="h-3.5" style={{ width: `${55 + ((i * 17) % 35)}%` }} />
            <Skeleton className="h-3 w-1/4" />
          </div>
          <Skeleton className="h-5 w-16 rounded-full" />
        </div>
      ))}
    </div>
  );
}

export function SkeletonCards({ count = 6, className }: { count?: number; className?: string }) {
  return (
    <div className={cn("grid gap-3 sm:grid-cols-2 xl:grid-cols-3", className)} aria-hidden>
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="rounded-card border border-border bg-surface p-4">
          <div className="flex items-center gap-3">
            <Skeleton className="size-9 rounded-lg" />
            <div className="flex-1 space-y-1.5">
              <Skeleton className="h-3.5 w-2/3" />
              <Skeleton className="h-3 w-1/3" />
            </div>
          </div>
          <SkeletonText lines={2} className="mt-4" />
        </div>
      ))}
    </div>
  );
}

export interface EmptyStateProps {
  icon?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
  compact?: boolean;
}

export function EmptyState({ icon, title, description, action, className, compact }: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center text-center",
        compact ? "gap-2 px-4 py-8" : "gap-3 px-6 py-14",
        className,
      )}
    >
      {icon && (
        <div
          className={cn(
            "flex items-center justify-center rounded-xl border border-border bg-surface-2 text-muted",
            compact ? "size-9 [&_svg]:size-4" : "size-11 [&_svg]:size-5",
          )}
        >
          {icon}
        </div>
      )}
      <h3 className={cn("font-serif-display text-fg", compact ? "text-xl" : "text-[26px] leading-tight")}>{title}</h3>
      {description && <p className="max-w-md text-[13px] leading-relaxed text-muted">{description}</p>}
      {action && <div className="mt-1 flex flex-wrap items-center justify-center gap-2">{action}</div>}
    </div>
  );
}

export function ErrorState({
  error,
  title,
  onRetry,
  className,
  compact,
}: {
  error: unknown;
  title?: string;
  onRetry?: () => void;
  className?: string;
  compact?: boolean;
}) {
  const network = error instanceof ApiError && error.isNetwork;
  const forbidden = error instanceof ApiError && error.isForbidden;
  return (
    <div
      role="alert"
      className={cn(
        "flex flex-col items-center justify-center gap-2 text-center",
        compact ? "px-4 py-6" : "px-6 py-12",
        className,
      )}
    >
      <div className="flex size-10 items-center justify-center rounded-xl tint-danger [&_svg]:size-5">
        {network ? <WifiOff /> : <AlertTriangle />}
      </div>
      <h3 className={cn("font-serif-display text-fg", compact ? "text-lg" : "text-2xl")}>
        {title ?? (network ? "Can't reach Ren" : forbidden ? "Not allowed" : "Something went wrong")}
      </h3>
      <p className="max-w-md text-[13px] text-muted">{errorMessage(error)}</p>
      {onRetry && !forbidden && (
        <Button size="sm" variant="secondary" icon={<RefreshCw className="size-3.5" />} onClick={onRetry} className="mt-1">
          Try again
        </Button>
      )}
    </div>
  );
}

export interface ProgressBarProps {
  value?: number | null;
  max?: number;
  tone?: "accent" | "success" | "warning" | "danger" | "info" | "fg";
  size?: "xs" | "sm" | "md";
  indeterminate?: boolean;
  className?: string;
  label?: string;
}

const fillTone: Record<NonNullable<ProgressBarProps["tone"]>, string> = {
  accent: "bg-accent",
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
  info: "bg-info",
  fg: "bg-fg",
};

export function ProgressBar({ value, max = 100, tone = "accent", size = "sm", indeterminate, className, label }: ProgressBarProps) {
  const pct = value === null || value === undefined ? 0 : Math.max(0, Math.min(100, (value / max) * 100));
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={indeterminate ? undefined : Math.round(pct)}
      className={cn(
        "relative w-full overflow-hidden rounded-full bg-surface-3",
        size === "xs" ? "h-1" : size === "sm" ? "h-1.5" : "h-2",
        className,
      )}
    >
      {indeterminate ? (
        <div
          className={cn("absolute inset-y-0 w-1/3 rounded-full", fillTone[tone])}
          style={{ animation: "progress-indeterminate 1.4s ease-in-out infinite" }}
        />
      ) : (
        <div
          className={cn("h-full rounded-full transition-[width] duration-300 ease-out", fillTone[tone])}
          style={{ width: `${pct}%` }}
        />
      )}
    </div>
  );
}

export function FullScreenLoader({ label = "Loading Ren…" }: { label?: string }) {
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-4 bg-bg">
      <div className="relative flex size-12 items-center justify-center">
        <svg viewBox="0 0 32 32" className="size-10 text-fg" aria-hidden>
          <g fill="none" stroke="currentColor" strokeWidth="2.1">
            <circle cx="16" cy="11.3" r="6.2" className="animate-pulse" />
            <circle cx="10.6" cy="20.7" r="6.2" className="animate-pulse [animation-delay:200ms]" />
            <circle cx="21.4" cy="20.7" r="6.2" className="animate-pulse [animation-delay:400ms]" />
          </g>
        </svg>
      </div>
      <p className="text-[13px] text-muted">{label}</p>
    </div>
  );
}

/** Small inline notice (info / warning / danger). */
export function Notice({
  tone = "info",
  title,
  children,
  icon,
  className,
  action,
}: {
  tone?: "info" | "warning" | "danger" | "success" | "neutral";
  title?: ReactNode;
  children?: ReactNode;
  icon?: ReactNode;
  className?: string;
  action?: ReactNode;
}) {
  return (
    <div
      className={cn(
        "flex items-start gap-3 rounded-card border px-3.5 py-3 text-[13px]",
        tone === "info" && "border-[color-mix(in_srgb,var(--info)_25%,transparent)] bg-[color-mix(in_srgb,var(--info)_7%,transparent)]",
        tone === "warning" && "border-[color-mix(in_srgb,var(--warning)_28%,transparent)] bg-[color-mix(in_srgb,var(--warning)_8%,transparent)]",
        tone === "danger" && "border-[color-mix(in_srgb,var(--danger)_28%,transparent)] bg-[color-mix(in_srgb,var(--danger)_7%,transparent)]",
        tone === "success" && "border-[color-mix(in_srgb,var(--success)_28%,transparent)] bg-[color-mix(in_srgb,var(--success)_7%,transparent)]",
        tone === "neutral" && "border-border bg-surface-2",
        className,
      )}
    >
      {icon && (
        <span
          className={cn(
            "mt-0.5 flex [&_svg]:size-4",
            tone === "info" && "text-info",
            tone === "warning" && "text-warning",
            tone === "danger" && "text-danger",
            tone === "success" && "text-success",
            tone === "neutral" && "text-muted",
          )}
        >
          {icon}
        </span>
      )}
      <div className="min-w-0 flex-1">
        {title && <p className="font-medium text-fg">{title}</p>}
        {children && <div className={cn("text-muted", title && "mt-0.5")}>{children}</div>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}
